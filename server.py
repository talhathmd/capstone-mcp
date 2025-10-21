# server.py — Bio MCP (Rhea + UniProt + optional ChEMBL), raw tools only
import os, re, asyncio
from typing import Any, Dict, List
import httpx
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route, Mount

# ---- Endpoints ----
UNIPROT = os.getenv("UNIPROT_SPARQL", "https://sparql.uniprot.org/sparql")
RHEA    = os.getenv("RHEA_SPARQL",    "https://sparql.rhea-db.org/sparql")
CHEMBL  = os.getenv("CHEMBL_SPARQL")  # optional

# ---- Headers / timeouts ----
UA        = os.getenv("BIO_UA",  "TalhaCapstone/0.7 (contact: you@example.com)")
FROM      = os.getenv("BIO_FROM", "")
FORCE_H1  = os.getenv("BIO_FORCE_HTTP1", "0") in ("1","true","True")
T_CONNECT = float(os.getenv("BIO_HTTP_TIMEOUT_CONNECT", "8"))
T_READ    = float(os.getenv("BIO_HTTP_TIMEOUT_READ",    "20"))
T_WRITE   = float(os.getenv("BIO_HTTP_TIMEOUT_WRITE",   "10"))

def _timeout():
    return httpx.Timeout(connect=T_CONNECT, read=T_READ, write=T_WRITE)

def _headers(for_get: bool = False) -> Dict[str,str]:
    h = {"Accept":"application/sparql-results+json","User-Agent":UA}
    if not for_get:
        h["Content-Type"] = "application/x-www-form-urlencoded"
    if FROM:
        h["From"] = FROM
    return h

def _sparql_str(s: str) -> str:
    return s.replace("\\","\\\\").replace('"','\\"')

async def _post_sparql(endpoint: str, query: str) -> Dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=_timeout(), follow_redirects=True, http2=not FORCE_H1) as client:
            r = await client.post(endpoint, data={"query": query}, headers=_headers())
        if r.status_code in (429,500,502,503,504):
            await asyncio.sleep(0.6)
            async with httpx.AsyncClient(timeout=_timeout(), follow_redirects=True, http2=not FORCE_H1) as client:
                r = await client.get(endpoint, params={"query": query}, headers=_headers(for_get=True))
        if r.status_code >= 400:
            return {"error": {"status_code": r.status_code, "body": r.text[:2000]}}
        return r.json()
    except (httpx.ReadTimeout, httpx.ConnectTimeout) as e:
        try:
            async with httpx.AsyncClient(timeout=_timeout(), follow_redirects=True, http2=not FORCE_H1) as client:
                r = await client.get(endpoint, params={"query": query}, headers=_headers(for_get=True))
            if r.status_code >= 400:
                return {"error": {"status_code": r.status_code, "body": r.text[:2000]}}
            return r.json()
        except Exception as e2:
            return {"error": {"status_code": 504, "body": f"Timeout after POST+GET fallback: {type(e2).__name__}: {e2}"}}
    except Exception as e:
        return {"error": {"status_code": 500, "body": f"{type(e).__name__}: {e}"}}

# ---------- MCP tools (raw) ----------
mcp = FastMCP("graph-bio")
# IMPORTANT: Keep the MCP route at "/" inside the sub-app, then mount at /mcp below
mcp.settings.streamable_http_path = "/"

@mcp.tool()
async def execute_sparql_uniprot(query_string: str, format: str = "json") -> Dict[str, Any]:
    return await _post_sparql(UNIPROT, query_string)

@mcp.tool()
async def execute_sparql_rhea(query_string: str, format: str = "json") -> Dict[str, Any]:
    return await _post_sparql(RHEA, query_string)

@mcp.tool()
async def execute_sparql_chembl(query_string: str, format: str = "json") -> Dict[str, Any]:
    if not CHEMBL:
        return {"error":{"status_code":400,"body":"CHEMBL_SPARQL not set"}}
    return await _post_sparql(CHEMBL, query_string)

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
  FILTER(CONTAINS(LCASE(STR(?label)), LCASE(?needle)) ||
         (BOUND(?acc) && CONTAINS(LCASE(STR(?acc)), LCASE(?needle))))
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
  FILTER(CONTAINS(LCASE(STR(?eq)), LCASE(?needle)) ||
         CONTAINS(LCASE(STR(?acc)), LCASE(?needle))))
}
ORDER BY ?acc
LIMIT %d
"""

async def _search_uniprot(needle: str, limit: int) -> List[Dict[str, Any]]:
    data = await _post_sparql(UNIPROT, UNIPROT_LABEL_SEARCH % (_sparql_str(needle), limit))
    if "error" in data:
        return [{"type":"error","id":"uniprot","title":"UniProt error","snippet":str(data["error"]),"source":"uniprot"}]
    out=[]
    for b in data.get("results",{}).get("bindings",[]):
        iri=b["id"]["value"]
        title=b.get("label",{}).get("value") or b.get("acc",{}).get("value") or iri.rsplit("/",1)[-1]
        out.append({"type":"uniprot:protein","id":iri,"title":title,"url":iri,"source":"uniprot"})
    return out

async def _search_rhea(needle: str, limit: int) -> List[Dict[str, Any]]:
    data = await _post_sparql(RHEA, RHEA_LABEL_SEARCH % (_sparql_str(needle), limit))
    if "error" in data:
        return [{"type":"error","id":"rhea","title":"Rhea error","snippet":str(data["error"]),"source":"rhea"}]
    out=[]
    for b in data.get("results",{}).get("bindings",[]):
        iri=b["id"]["value"]; acc=b.get("acc",{}).get("value",""); eq=b.get("eq",{}).get("value","")
        out.append({"type":"rhea:reaction","id":iri,"title":f"{acc} — {eq[:160]}","url":iri,"source":"rhea"})
    return out

async def _search_chembl(needle: str, limit: int) -> List[Dict[str, Any]]:
    if not CHEMBL:
        return [{"type":"error","id":"chembl","title":"ChEMBL not configured","snippet":"Set CHEMBL_SPARQL env var","source":"chembl"}]
    q = f"""
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?id ?label WHERE {{
  BIND(\"\"\"{_sparql_str(needle)}\"\"\" AS ?needle)
  ?id rdfs:label ?label .
  FILTER(CONTAINS(LCASE(STR(?label)), LCASE(?needle)))
}}
LIMIT {limit}
"""
    data = await _post_sparql(CHEMBL, q)
    if "error" in data:
        return [{"type":"error","id":"chembl","title":"ChEMBL error","snippet":str(data["error"]),"source":"chembl"}]
    out=[]
    for b in data.get("results",{}).get("bindings",[]):
        iri=b["id"]["value"]; label=b.get("label",{}).get("value","(no label)")
        out.append({"type":"chembl:any","id":iri,"title":label,"url":iri,"source":"chembl"})
    return out

@mcp.tool(name="search", description="Search Rhea/UniProt/ChEMBL by label or ID.")
async def search(query: str, limit: int = 10, language: str = "en", source: str = "both"):
    src=(source or "both").lower()
    tasks=[]
    if src in ("uniprot","both","all"): tasks.append(_search_uniprot(query, limit))
    if src in ("rhea","both","all"):    tasks.append(_search_rhea(query, limit))
    if src in ("chembl","both","all"):  tasks.append(_search_chembl(query, limit))
    results=[]
    for coro in tasks:
        try: results += await asyncio.wait_for(coro, timeout=T_CONNECT+T_READ+2)
        except Exception as e:
            results += [{"type":"error","id":"search","title":"timeout","snippet":str(e),"source":"search"}]
    # de-dup
    seen=set(); dedup=[]
    for r in results:
        rid=(r.get("id"), r.get("source"))
        if rid not in seen:
            seen.add(rid); dedup.append(r)
    out={"results": dedup[:limit]}
    # bubble any errors separately
    errs=[r for r in results if r.get("type")=="error"]
    if errs: out["errors"]=errs
    return out

@mcp.tool(name="fetch", description="Fetch by URL, UniProt accession (e.g., P00533), or RHEA:ID.")
async def fetch(id: str, language: str = "en"):
    s=(id or "").strip()
    try:
        if re.match(r"^https?://", s, re.IGNORECASE):
            async with httpx.AsyncClient(timeout=_timeout()) as client:
                r=await client.get(s, headers={"User-Agent": UA})
            return {"id": s, "url": s, "mime": r.headers.get("content-type"), "content": r.text[:200000]}
        m=re.match(r"^RHEA:(\d+)$", s, re.IGNORECASE)
        if m:
            iri=f"https://rdf.rhea-db.org/{m.group(1)}"
            async with httpx.AsyncClient(timeout=_timeout()) as client:
                r=await client.get(iri, headers={"User-Agent": UA})
            return {"id": s, "url": iri, "mime": r.headers.get("content-type"), "content": r.text[:200000]}
        if re.match(r"^[A-Z0-9]{6,10}(?:-\\d+)?$", s):
            iri=f"https://purl.uniprot.org/uniprot/{s}"
            async with httpx.AsyncClient(timeout=_timeout()) as client:
                r=await client.get(iri, headers={"User-Agent": UA})
            return {"id": s, "url": iri, "mime": r.headers.get("content-type"), "content": r.text[:200000]}
        return {"error":"Pass a URL, a UniProt accession (e.g., P00533), or RHEA:12345."}
    except Exception as e:
        return {"error": f"Fetch failed: {type(e).__name__}: {e}"}

BIO_HINTS_UNIPROT=("uniprot","protein","proteome","isoform","mnemonic","go:","ec ","ec:","enzyme","kinase","receptor","domain")
BIO_HINTS_RHEA   =("rhea","reaction","substrate","product","equation","balanced","transport","stoichiometry","reversible","irreversible")
BIO_HINTS_CHEMBL =("chembl","compound","molecule","drug","ligand","smiles","inchi","assay","bioactivity")

@mcp.tool(name="choose_endpoint", description="Return the best KG: 'uniprot'|'rhea'|'chembl'.")
async def choose_endpoint(question: str) -> Dict[str, Any]:
    q=(question or "").lower()
    if any(k in q for k in BIO_HINTS_RHEA):   return {"target":"rhea","reason":"reaction cues detected"}
    if any(k in q for k in BIO_HINTS_CHEMBL): return {"target":"chembl","reason":"compound/bioactivity cues detected"}
    if any(k in q for k in BIO_HINTS_UNIPROT):return {"target":"uniprot","reason":"protein cues detected"}
    return {"target":"uniprot","reason":"default fallback"}

@mcp.tool(name="debug_ping", description="SELECT 1 against each configured endpoint.")
async def debug_ping():
    async def ping(ep):
        if not ep: return {"skipped": True}
        return await _post_sparql(ep, "SELECT (1 AS ?x) WHERE {}")
    return {"uniprot": await ping(UNIPROT), "rhea": await ping(RHEA), "chembl": await ping(CHEMBL)}

# ---- Build composite ASGI app: /healthz and /mcp ----
mcp_app = mcp.streamable_http_app()

async def healthz(_):
    return JSONResponse({"ok": True, "service": "graph-bio", "paths": ["/healthz", "/mcp"]})

# NOTE: MCP lives under /mcp (root remains free for simple health/docs)
app = Starlette(routes=[
    Route("/healthz", endpoint=healthz, methods=["GET"]),
    Route("/", endpoint=lambda req: PlainTextResponse("OK. MCP endpoint is at /mcp.", status_code=200), methods=["GET"]),
    Mount("/mcp", app=mcp_app),
])

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")), reload=True)