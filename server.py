# MCP server for Rhea SPARQL queries
import os
import re
import asyncio
from typing import Any, Dict, List, Optional
import httpx
from mcp.server.fastmcp import FastMCP

RHEA_SPARQL = os.getenv("RHEA_SPARQL", "https://sparql.rhea-db.org/sparql")
UA          = os.getenv("BIO_UA", "GraphBio-RheaOnly/2.1 (contact: you@example.com)")

mcp = FastMCP("graph-bio-rhea")
mcp.settings.streamable_http_path = "/"

# HTTP/2 support check
try:
    import h2  # type: ignore
    _H2_AVAILABLE = True
except Exception:
    _H2_AVAILABLE = False

def _http2_enabled() -> bool:
    """
    BIO_HTTP2:
      off/false/0/no  -> HTTP/1.1
      on/true/1/yes   -> HTTP/2 if installed, else HTTP/1.1
      auto (default)  -> HTTP/2 only if 'h2' is installed
    """
    mode = (os.getenv("BIO_HTTP2", "auto") or "").lower()
    if mode in ("off", "false", "0", "no"):
        return False
    if mode in ("on", "true", "1", "yes"):
        return _H2_AVAILABLE
    return _H2_AVAILABLE

async def _exec_sparql_json(endpoint: str, query: str, timeout: float = 60.0) -> Dict[str, Any]:
    """
    Execute SPARQL expecting JSON (SELECT/ASK).
    Fallback matrix:
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

    attempts = [_do_post(False), _do_post(True), _do_get(False), _do_get(True)]
    for coro in attempts:
        try:
            res = await coro
            if "error" not in res:
                return res
        except (httpx.TimeoutException, httpx.TransportError):
            continue

    return {"error": {"status_code": 599, "body": "All SPARQL attempts failed"}}

def _escape_for_contains(s: str) -> str:
    # Need to escape these for SPARQL string literals
    return s.replace("\\", "\\\\").replace('"', '\\"')

def _mk_limit(limit: Optional[int], default: int = 200) -> int:
    try:
        n = int(limit) if limit is not None else default
        return max(1, min(n, 2000))
    except Exception:
        return default
@mcp.tool(
    name="reactions_producing_product_from_substrate_names",
    description=(
        "Find APPROVED Rhea reactions that convert a given substrate name to a given product name.\n"
        "Matches by compound names (case-insensitive, contains).\n"
        "Directionality is enforced via rh:transformableTo (left→right).\n\n"
        "Args:\n"
        "  substrate_name: e.g. \"L-glutamine\"\n"
        "  product_name:   e.g. \"ammonia\"\n"
        "  limit:          optional integer (default 200)\n"
        "Returns: ?reaction IRI and ?equation string."
    )
)
async def reactions_producing_product_from_substrate_names(
    substrate_name: str,
    product_name: str,
    limit: Optional[int] = None
):
    s_name = _escape_for_contains(substrate_name or "")
    p_name = _escape_for_contains(product_name or "")
    lim = _mk_limit(limit, 200)
    if not s_name or not p_name:
        return {"error": "Provide both substrate_name and product_name"}

    q = f"""
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX rh:   <http://rdf.rhea-db.org/>

SELECT DISTINCT ?reaction ?equation WHERE {{
  ?reaction rdfs:subClassOf rh:Reaction ;
            rh:status rh:Approved ;
            rh:equation ?equation ;
            rh:side ?left, ?right .
  ?left  rh:transformableTo ?right .

  ?left  rh:contains ?p1 .
  ?p1    rh:compound ?c1 .
  ?c1    rh:name ?n1 .
  FILTER(CONTAINS(LCASE(STR(?n1)), "{s_name.lower()}"))

  ?right rh:contains ?p2 .
  ?p2    rh:compound ?c2 .
  ?c2    rh:name ?n2 .
  FILTER(CONTAINS(LCASE(STR(?n2)), "{p_name.lower()}"))
}}
ORDER BY ?reaction
LIMIT {lim}
"""
    return await _exec_sparql_json(RHEA_SPARQL, q)


@mcp.tool(
    name="reactions_by_ec",
    description=(
        "Find APPROVED Rhea reactions for a given EC number.\n"
        "Args:\n"
        "  ec_number: string like '1.11.1.6'\n"
        "  limit: optional integer (default 200)\n"
        "Returns: ?reaction and ?equation."
    )
)
async def reactions_by_ec(ec_number: str, limit: Optional[int] = None):
    num = (ec_number or "").strip()
    lim = _mk_limit(limit, 200)
    if not re.match(r"^\d+\.\d+\.\d+\.\d+$", num):
        return {"error": "Invalid EC number format (expected a.b.c.d)"}
    q = f"""
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX rh:   <http://rdf.rhea-db.org/>
PREFIX ec:   <http://purl.uniprot.org/enzyme/>

SELECT ?reaction ?equation WHERE {{
  ?reaction rdfs:subClassOf rh:Reaction ;
            rh:status rh:Approved ;
            rh:equation ?equation ;
            rh:ec ec:{num} .
}}
ORDER BY ?reaction
LIMIT {lim}
"""
    return await _exec_sparql_json(RHEA_SPARQL, q)


@mcp.tool(
    name="find_reaction_by_equation_text",
    description=(
        "Search reactions by equation text (case-insensitive substring match on rh:equation).\n"
        "Args:\n"
        "  contains_text: e.g. 'alcohol + NAD+' or '2 H2O2'\n"
        "  limit: optional integer (default 50)\n"
        "Returns: ?reaction, ?accession, ?equation."
    )
)
async def find_reaction_by_equation_text(contains_text: str, limit: Optional[int] = None):
    text = _escape_for_contains(contains_text or "")
    lim = _mk_limit(limit, 50)
    if not text:
        return {"error": "Provide contains_text"}

    q = f"""
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX rh:   <http://rdf.rhea-db.org/>

SELECT ?reaction ?accession ?equation WHERE {{
  ?reaction rdfs:subClassOf rh:Reaction ;
            rh:accession ?accession ;
            rh:equation  ?equation .
  FILTER(CONTAINS(LCASE(STR(?equation)), "{text.lower()}"))
}}
LIMIT {lim}
"""
    return await _exec_sparql_json(RHEA_SPARQL, q)


@mcp.tool(
    name="children_of_reaction",
    description=(
        "Fetch specific child reactions of a given parent reaction (by RHEA:<digits>), "
        "following rdfs:subClassOf+.\n"
        "Args:\n"
        "  parent_rhea_id: e.g. 'RHEA:12345'\n"
        "  limit: optional integer (default 500)\n"
        "Returns: ?child and ?childEq."
    )
)
async def children_of_reaction(parent_rhea_id: str, limit: Optional[int] = None):
    lim = _mk_limit(limit, 500)
    m = re.match(r"^RHEA:(\d+)$", (parent_rhea_id or "").strip(), re.IGNORECASE)
    if not m:
        return {"error": "Provide parent_rhea_id like 'RHEA:12345'"}
    num = m.group(1)
    q = f"""
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX rh:   <http://rdf.rhea-db.org/>

SELECT ?child ?childEq WHERE {{
  VALUES (?parent) {{ (rh:{num}) }}
  ?child rdfs:subClassOf+ ?parent ;
         rh:equation ?childEq .
}}
ORDER BY ?child
LIMIT {lim}
"""
    return await _exec_sparql_json(RHEA_SPARQL, q)

@mcp.tool(
    name="execute_sparql_rhea",
    description=(
        "Run a SELECT/ASK SPARQL query against the Rhea endpoint and return JSON only. "
        "YOU (the AI) must translate NL → SPARQL.\n"
        "Contract:\n"
        "• Prefixes to use: "
        "  PREFIX rh:   <http://rdf.rhea-db.org/>  "
        "  PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>  "
        "  PREFIX ec:   <http://purl.uniprot.org/enzyme/>  "
        "  PREFIX CHEBI: <http://purl.obolibrary.org/obo/CHEBI_>  "
        "  PREFIX pubmed: <http://rdf.ncbi.nlm.nih.gov/pubmed/>\n"
        "• Reactions: ?r rdfs:subClassOf rh:Reaction .  IDs/text: ?r rh:accession ?acc ; rh:equation ?eq .  "
        "  Optional: ?r rh:status rh:Approved ; rh:isTransport ?isTransport .\n"
        "• EC mapping: ?r rh:ec ?ec .  (bind/display EC string from the ec: namespace IRI) \n"
        "• Participants: ?r rh:side ?s . ?s rh:contains ?part . ?part rh:compound ?c . "
        "  Compound binding (any of): ?c (rh:chebi | rh:underlyingChebi | (rh:reactivePart/rh:chebi)) ?chebi . "
        "  Names: ?c rh:name ?name .\n"
        "• Directionality: identify left/right sides via ?left rh:transformableTo ?right . "
        "  Use left for consumers and right for products.\n"
        "• Cross-references (xrefs): use rdfs:seeAlso on the reaction itself OR on its rh:directionalReaction OR on its rh:bidirectionalReaction. "
        "  Filter by target source (e.g., KEGG/MetaCyc/Reactome/MACiE) using string/regex on the IRI.\n"
        "• Citations: ?r rh:citation ?pm .  (PubMed IRIs) \n"
        "• Descendants/specific forms: ?child rdfs:subClassOf+ ?parent .\n"
        "• Quartet mapping: collect forms via rh:directionalReaction and rh:bidirectionalReaction.\n"
        "• CHEBI class queries: resolve a CHEBI class by rdfs:label, then expand with ?x rdfs:subClassOf+ ?class before matching participants.\n"
        "• Always include a LIMIT and escape double quotes in string literals."
    )
)
async def execute_sparql_rhea(query_string: str, timeout: float = 60.0) -> Dict[str, Any]:
    s = (query_string or "").strip()
    if not s:
        return {"error": "Empty SPARQL query."}
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
    description="Test connection to Rhea endpoint and show HTTP/2 status."
)
async def debug_ping():
    use_h2 = _http2_enabled()
    rh = await _exec_sparql_json(RHEA_SPARQL, "SELECT (1 AS ?x) WHERE {}", timeout=10.0)
    return {"rhea": rh, "http2_enabled": use_h2, "h2_installed": _H2_AVAILABLE}

sse_app = mcp.streamable_http_app()

async def _plain_200(scope, receive, send, body: str = "MCP server OK (root)."):
    headers = [
        (b"content-type", b"text/plain; charset=utf-8"),
        (b"cache-control", b"no-store"),
        (b"access-control-allow-origin", b"*"),
    ]
    await send({"type": "http.response.start", "status": 200, "headers": headers})
    await send({"type": "http.response.body", "body": body.encode("utf-8"), "more_body": False})

class RootOrSSE:
    # Route requests: health check, SSE for MCP, or plain 200
    def __init__(self, sse):
        self.sse = sse

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.sse(scope, receive, send)

        path = scope.get("path", "/") or "/"
        hdrs = {k.decode("latin1").lower(): v.decode("latin1") for k, v in scope.get("headers", [])}
        accept = hdrs.get("accept", "")

        if path == "/healthz":
            return await _plain_200(scope, receive, send, "ok")

        if "text/event-stream" in accept:
            return await self.sse(scope, receive, send)

        return await _plain_200(scope, receive, send)

app = RootOrSSE(sse_app)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")), reload=True)