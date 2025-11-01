# server.py — Rhea-only MCP for NL→SPARQL:
import os
import re
import asyncio
from typing import Any, Dict
import httpx
from mcp.server.fastmcp import FastMCP

RHEA_SPARQL = os.getenv("RHEA_SPARQL", "https://sparql.rhea-db.org/sparql")
UA          = os.getenv("BIO_UA", "GraphBio-RheaOnly/2.0 (contact: you@example.com)")

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
    BIO_HTTP2 env:
      - off/false/0/no  -> force HTTP/1.1
      - on/true/1/yes   -> use HTTP/2 if installed, else HTTP/1.1
      - auto (default)  -> use HTTP/2 only if 'h2' is installed
    """
    mode = (os.getenv("BIO_HTTP2", "auto") or "").lower()
    if mode in ("off", "false", "0", "no"):
        return False
    if mode in ("on", "true", "1", "yes"):
        return _H2_AVAILABLE
    return _H2_AVAILABLE

# -------------------- SPARQL transport --------------------
async def _exec_sparql_json(endpoint: str, query: str, timeout: float = 60.0) -> Dict[str, Any]:
    """
    Execute a SPARQL query expecting a JSON result (SELECT/ASK).
    Robust fallback matrix:
      1) POST (no format)
      2) POST (format=json)
      3) GET  (no format)
      4) GET  (format=json)
    """
    use_h2 = _http2_enabled()
    t = httpx.Timeout(connect=10.0, read=timeout, write=30.0, pool=10.0)

    accept = "application/sparql-results+json"
    headers_post = {"Accept": accept, "User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"}
    headers_get  = {"Accept": accept, "User-Agent": UA}

    async def _do_post(with_format: bool):
        data = {"query": query}
        if with_format:
            data["format"] = "json"
        async with httpx.AsyncClient(timeout=t, follow_redirects=True, http2=use_h2) as client:
            r = await client.post(endpoint, data=data, headers=headers_post)
            if r.status_code >= 400:
                return {"error": {"status_code": r.status_code, "body": (r.text or "")[:2000]}}
            return r.json()

    async def _do_get(with_format: bool):
        params = {"query": query}
        if with_format:
            params["format"] = "json"
        async with httpx.AsyncClient(timeout=t, follow_redirects=True, http2=use_h2) as client:
            r = await client.get(endpoint, params=params, headers=headers_get)
            if r.status_code >= 400:
                return {"error": {"status_code": r.status_code, "body": (r.text or "")[:2000]}}
            return r.json()

    attempts = [
        _do_post(False),
        _do_post(True),
        _do_get(False),
        _do_get(True),
    ]

    # Try each transport/format combo in order; stop on first non-error JSON
    for coro in attempts:
        try:
            res = await coro
            if "error" not in res:
                return res
        except (httpx.TimeoutException, httpx.TransportError):
            # try next attempt
            continue

    return {"error": {"status_code": 599, "body": "All SPARQL attempts failed"}}

# -------------------- Tools --------------------
@mcp.tool(
    name="execute_sparql_rhea",
    description=(
        "Run a SPARQL query against the Rhea SPARQL endpoint.\n\n"
        "YOU (the AI) MUST convert the user's natural-language request into SPARQL.\n"
        "Contract:\n"
        "  • Use SELECT or ASK only (JSON results expected).\n"
        "  • Prefer these prefixes:\n"
        "      PREFIX rh:   <http://rdf.rhea-db.org/>\n"
        "      PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"
        "  • Reaction classes are rdfs:subClassOf rh:Reaction.\n"
        "  • Reaction accession is rh:accession; human-readable equation is rdfs:label.\n"
        "  • For text search, use LCASE/CONTAINS on STR(?eq) and/or STR(?acc).\n"
        "  • Always include a LIMIT.\n"
        "  • Escape double quotes in string literals.\n\n"
        "Return columns are up to you, but typical ones include ?id ?acc ?eq.\n"
        "If the user asks for counts, use COUNT(*). If yes/no, use ASK.\n"
        "If the user asks for a specific Rhea accession, match it via rh:accession.\n"
    )
)
async def execute_sparql_rhea(query_string: str, timeout: float = 60.0) -> Dict[str, Any]:
    """
    Execute caller-provided SPARQL (SELECT/ASK) and return JSON results.
    """
    s = (query_string or "").strip()
    if not s:
        return {"error": "Empty SPARQL query."}
    # Very light guardrails: discourage CONSTRUCT/DESCRIBE to keep JSON contract.
    if re.search(r"\b(CONSTRUCT|DESCRIBE)\b", s, flags=re.IGNORECASE):
        return {"error": "Use SELECT or ASK for JSON results."}
    return await _exec_sparql_json(RHEA_SPARQL, s, timeout=timeout)

@mcp.tool(
    name="fetch",
    description="Fetch raw content for a Rhea accession (RHEA:<digits>) or an HTTP(S) URL."
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
    rh = await _exec_sparql_json(RHEA_SPARQL, "SELECT (1 AS ?x) WHERE {}", timeout=10.0)
    return {"rhea": rh, "http2_enabled": use_h2, "h2_installed": _H2_AVAILABLE}

# -------------------- ASGI app --------------------
app = mcp.streamable_http_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")), reload=True)
