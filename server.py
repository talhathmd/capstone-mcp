# server.py — Bio MCP (Rhea + UniProt), root "/" only, NO "answer" tool
import os
import re
import asyncio
from typing import Any, Dict, List
import httpx
from mcp.server.fastmcp import FastMCP

UNIPROT = os.getenv("UNIPROT_SPARQL", "https://sparql.uniprot.org/sparql")
RHEA    = os.getenv("RHEA_SPARQL",    "https://sparql.rhea-db.org/sparql")
UA      = os.getenv("BIO_UA", "GraphBio/1.1 (contact: you@example.com)")

mcp = FastMCP("graph-bio")
mcp.settings.streamable_http_path = "/"  # MCP lives at root

# -------------------- Helpers --------------------

_MAX_TOKENS = 6            # avoid huge WHEREs
_MAX_TOKEN_LEN = 64        # avoid pathological very-long tokens

def _sparql_str(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace('"', '\\"')

def _nl_tokens(q: str) -> List[str]:
    """
    Tokenize safely:
      - letters/digits/_/-
      - drop short tokens (<=2)
      - downcase
      - cap count/length
    """
    toks = re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]+", q or "")
    toks = [t.lower()[:_MAX_TOKEN_LEN] for t in toks if len(t) >= 3]
    # dedupe preserving order
    seen = set()
    out = []
    for t in toks:
        if t not in seen:
            seen.add(t); out.append(t)
        if len(out) >= _MAX_TOKENS:
            break
    return out

def _build_uniprot_text_query_full(q: str, limit: int) -> str:
    """
    Free-text search with optional organism join.
    AND across tokens; match against protein label OR organism label.
    """
    tokens = _nl_tokens(q)
    token_filters = "\n".join(
        f'  FILTER(CONTAINS(?lcLabel, "{_sparql_str(t)}") OR CONTAINS(?lcOrg, "{_sparql_str(t)}"))'
        for t in tokens
    ) or "  # no tokens; return limited results\n"

    return f"""
PREFIX up:   <http://purl.uniprot.org/core/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?id ?acc ?label ?orgLabel WHERE {{
  ?id a up:Protein .
  OPTIONAL {{ ?id up:mnemonic ?acc . }}
  OPTIONAL {{ ?id up:recommendedName/up:fullName ?rn . }}
  OPTIONAL {{ ?id rdfs:label ?rl . }}
  BIND(COALESCE(?rn, ?rl, ?acc) AS ?label)

  OPTIONAL {{
    ?id up:organism ?org .
    OPTIONAL {{
      ?org rdfs:label ?orgLabel .
      FILTER(LANG(?orgLabel) = "" || LANGMATCHES(LANG(?orgLabel), "en"))
    }}
  }}

  BIND(LCASE(STR(?label)) AS ?lcLabel)
  BIND(LCASE(STR(COALESCE(?orgLabel, ""))) AS ?lcOrg)
{token_filters}
}}
ORDER BY ?label
LIMIT {int(limit)}
""".strip()

def _build_uniprot_text_query_minimal(q: str, limit: int) -> str:
    """
    Simpler fallback: no organism join, fewer OPTIONALs.
    AND across tokens; match protein label or mnemonic.
    """
    tokens = _nl_tokens(q)
    token_filters = "\n".join(
        f'  FILTER(CONTAINS(?lcLabel, "{_sparql_str(t)}") OR CONTAINS(?lcAcc, "{_sparql_str(t)}"))'
        for t in tokens
    ) or "  # no tokens; return limited results\n"

    return f"""
PREFIX up:   <http://purl.uniprot.org/core/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?id ?acc ?label WHERE {{
  ?id a up:Protein .
  OPTIONAL {{ ?id up:mnemonic ?acc . }}
  OPTIONAL {{ ?id up:recommendedName/up:fullName ?rn . }}
  OPTIONAL {{ ?id rdfs:label ?rl . }}
  BIND(COALESCE(?rn, ?rl, ?acc) AS ?label)

  BIND(LCASE(STR(?label)) AS ?lcLabel)
  BIND(LCASE(STR(COALESCE(?acc, ""))) AS ?lcAcc)
{token_filters}
}}
ORDER BY ?label
LIMIT {int(limit)}
""".strip()

async def _post_sparql(
    endpoint: str,
    query: str,
    timeout: float = 60.0,
    retries: int = 2,
    prefer_get_fallback: bool = True,
    force_http1: bool = False,   # prefer HTTP/2; some gateways behave better
) -> Dict[str, Any]:
    """
    POST SPARQL with fallback to GET (and format hint), exponential backoff,
    and HTTP/2 by default.
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
        async with httpx.AsyncClient(timeout=t, follow_redirects=True, http2=not force_http1) as client:
            # add 'format=json' which some proxies/gateways prefer
            r = await client.post(endpoint, data={"query": query, "format": "json"}, headers=headers_post)
            if r.status_code >= 400:
                return {"error": {"status_code": r.status_code, "body": (r.text or "")[:2000]}}
            return r.json()

    async def _try_get():
        async with httpx.AsyncClient(timeout=t, follow_redirects=True, http2=not force_http1) as client:
            r = await client.get(endpoint, params={"query": query, "format": "json"}, headers=headers_get)
            if r.status_code >= 400:
                return {"error": {"status_code": r.status_code, "body": (r.text or "")[:2000]}}
            return r.json()

    for attempt in range(retries + 1):
        try:
            return await _try_post()
        except (httpx.TimeoutException, httpx.TransportError):
            # transport failure; try again/backoff
            pass
        await asyncio.sleep(backoff)
        backoff *= 2

    # exhausted POST attempts; try GET once (classic corporate proxies)
    if prefer_get_fallback:
        try:
            return await _try_get()
        except (httpx.TimeoutException, httpx.TransportError) as e2:
            return {"error": {"status_code": 599, "body": f"GET fallback {type(e2).__name__}: {e2}"}}
    return {"error": {"status_code": 599, "body": "SPARQL POST failed and GET not attempted"}}

# -------------------- Raw SPARQL tools --------------------

@mcp.tool()
async def execute_sparql_uniprot(query_string: str, format: str = "json") -> Dict[str, Any]:
    """Run a SPARQL query against the UniProt endpoint."""
    return await _post_sparql(UNIPROT, query_string, timeout=60.0)

@mcp.tool()
async def execute_sparql_rhea(query_string: str, format: str = "json") -> Dict[str, Any]:
    """Run a SPARQL query against the Rhea endpoint."""
    return await _post_sparql(RHEA, query_string, timeout=60.0)

# -------------------- Label search builders --------------------

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

MNEMONIC_EQ_Q = """
PREFIX up: <http://purl.uniprot.org/core/>
SELECT ?id ?acc WHERE {
  ?id a up:Protein ; up:mnemonic ?acc .
  FILTER(?acc = \"%s\")
}
LIMIT %d
"""

def _is_mnemonic_like(s: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{3,20}", s or ""))

async def _search_uniprot_labels(needle: str, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Try mnemonic equality -> full query -> minimal fallback if UniProt returns 400.
    """
    # 1) exact mnemonic equality (index-friendly)
    if _is_mnemonic_like(needle):
        q_fast = MNEMONIC_EQ_Q % (_sparql_str(needle), limit)
        data_fast = await _post_sparql(UNIPROT, q_fast, timeout=90.0)
        if "error" not in data_fast:
            out_fast: List[Dict[str, Any]] = []
            for b in data_fast.get("results", {}).get("bindings", []):
                iri = b["id"]["value"]
                acc = b.get("acc", {}).get("value") or iri.rsplit("/", 1)[-1]
                out_fast.append({
                    "type": "uniprot:protein",
                    "id": iri, "title": acc, "snippet": "UniProtKB protein",
                    "url": iri, "source": "uniprot"
                })
            if out_fast:
                return out_fast
        # if error, just continue to robust path

    # 2) full text+organism query
    q_full = _build_uniprot_text_query_full(needle, limit)
    data = await _post_sparql(UNIPROT, q_full, timeout=90.0)
    if "error" in data and data["error"].get("status_code") == 400:
        # 3) minimal fallback if their gateway rejects the full query
        q_min = _build_uniprot_text_query_minimal(needle, limit)
        data = await _post_sparql(UNIPROT, q_min, timeout=90.0)

    if "error" in data:
        return [{
            "type": "error", "id": "uniprot", "title": "UniProt SPARQL error",
            "snippet": f"{data['error'].get('status_code')} — {data['error'].get('body','')[:160]}",
            "url": UNIPROT, "source": "uniprot"
        }]

    out: List[Dict[str, Any]] = []
    for b in data.get("results", {}).get("bindings", []):
        iri   = b["id"]["value"]
        acc   = b.get("acc", {}).get("value")
        label = b.get("label", {}).get("value") or acc or iri.rsplit("/", 1)[-1]
        out.append({
            "type": "uniprot:protein",
            "id": iri, "title": label, "snippet": "UniProtKB protein",
            "url": iri, "source": "uniprot"
        })
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
        out.append({
            "type": "rhea:reaction",
            "id": iri, "title": title, "snippet": "Rhea reaction",
            "url": iri, "source": "rhea"
        })
    return out

# -------------------- Public search/fetch --------------------

@mcp.tool(
    name="search",
    description=(
        "Searches UniProt proteins and Rhea reactions by label or identifier. "
        "Understands free-text as well as compact identifiers; 'source' can be 'uniprot', 'rhea', or 'both' (default)."
    )
)
async def search(query: str, limit: int = 10, language: str = "en", source: str = "both"):
    """
    Behavior:
      1) Exact-ID routing:
         - Rhea accessions (RHEA:<digits>)
         - UniProt accessions (6–10 chars, optional isoform suffix)
         - UniProt mnemonics (compact tokens with underscore)
      2) Otherwise, run label searches against selected source(s).
    """
    src = (source or "both").lower()
    s = (query or "").strip()
    results: List[Dict[str, Any]] = []

    # --- 1) Exact routing ---
    m_rhea = re.fullmatch(r"(?i)RHEA:(\d+)", s)
    if m_rhea:
        rid = m_rhea.group(1)
        iri = f"https://rdf.rhea-db.org/{rid}"
        return {"results": [{
            "type": "rhea:reaction", "id": iri, "title": f"RHEA:{rid}",
            "snippet": "Rhea reaction", "url": iri, "source": "rhea"
        }][:limit]}

    if re.fullmatch(r"[A-NR-Z0-9]{6,10}(?:-\d+)?", s):
        iri = f"https://purl.uniprot.org/uniprot/{s}"
        return {"results": [{
            "type": "uniprot:protein", "id": iri, "title": s,
            "snippet": "UniProtKB protein (accession)", "url": iri, "source": "uniprot"
        }][:limit]}

    if "_" in s and re.fullmatch(r"[A-Za-z0-9_-]{3,20}", s or ""):
        hits = await _search_uniprot_labels(s, limit=limit)
        # if mnemonic equality or text finds something, return
        if hits and all(h.get("type") != "error" for h in hits):
            return {"results": hits[:limit]}

    # --- 2) Label searches ---
    if src in ("uniprot", "both", "all"):
        results += await _search_uniprot_labels(s, limit=limit)
    if src in ("rhea", "both", "all"):
        results += await _search_rhea_labels(s, limit=limit)

    # --- combine, dedupe, surface errors ---
    errors, ok = [], []
    for r in results:
        if r.get("type") == "error":
            errors.append({
                "source": r.get("id"),
                "message": r.get("snippet"),
                "endpoint": r.get("url")
            })
        else:
            ok.append(r)

    dedup = []
    seen = set()
    for r in ok:
        rid = r.get("id") or r.get("url")
        if rid and rid not in seen:
            seen.add(rid); dedup.append(r)

    out: Dict[str, Any] = {"results": dedup[:limit]}
    if errors:
        out["errors"] = errors
    return out

@mcp.tool(
    name="fetch",
    description=(
        "Fetch content for a given identifier or URL. "
        "Accepts Rhea accessions (RHEA:<digits>), UniProt accessions (with optional isoform suffix), "
        "or HTTP(S) URLs."
    )
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
            return {"error": f"Fetch failed for Rhea ID: {e}"}
    if re.match(r"^[A-Z0-9]{6,10}(?:-\\d+)?$", s):
        iri = f"https://purl.uniprot.org/uniprot/{s}"
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, http2=True) as client:
                r = await client.get(iri, headers={"User-Agent": UA})
            return {"id": s, "url": iri, "mime": r.headers.get("content-type"), "content": r.text[:200000]}
        except Exception as e:
            return {"error": f"Fetch failed for UniProt accession: {e}"}
    return {"error": "Provide a URL, a UniProt accession, or a Rhea accession."}

# -------------------- Endpoint chooser --------------------

BIO_HINTS_UNIPROT = (
    "uniprot", "protein", "proteome", "isoform", "mnemonic", "go:", "ec ", "ec:", "enzyme",
    "kinase", "receptor", "domain", "signal peptide", "transmembrane",
)
BIO_HINTS_RHEA = (
    "rhea", "reaction", "substrate", "product", "equation", "balanced", "transport",
    "stoichiometry", "reversible", "irreversible",
)

@mcp.tool(
    name="choose_endpoint",
    description="Suggests 'uniprot' or 'rhea' based on lexical cues in a natural-language question."
)
async def choose_endpoint(question: str) -> Dict[str, Any]:
    q = (question or "").lower()
    if any(k in q for k in BIO_HINTS_RHEA):
        return {"target": "rhea", "reason": "reaction-related cues detected"}
    if any(k in q for k in BIO_HINTS_UNIPROT):
        return {"target": "uniprot", "reason": "protein-related cues detected"}
    return {"target": "uniprot", "reason": "default fallback"}

# -------------------- Diagnostics --------------------

@mcp.tool(name="debug_ping", description="Simple SELECT 1 checks for both endpoints.")
async def debug_ping():
    up = await _post_sparql(UNIPROT, "SELECT (1 AS ?x) WHERE {}", timeout=10.0, retries=0)
    rh = await _post_sparql(RHEA,   "SELECT (1 AS ?x) WHERE {}", timeout=10.0, retries=0)
    return {"uniprot": up, "rhea": rh}

# -------------------- ASGI app --------------------

app = mcp.streamable_http_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")), reload=True)
