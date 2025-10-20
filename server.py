# server.py — Bio-only MCP (UniProt + Rhea), no local DB, no sports/Wikidata
import os
import re
from typing import Any, Dict, List
import httpx
from mcp.server.fastmcp import FastMCP

UNIPROT = "https://sparql.uniprot.org/sparql"
RHEA = "https://sparql.rhea-db.org/sparql"
UA = os.getenv("BIO_UA", "TalhaCapstone/0.4 (contact: talhatah2022@gmail.com)")

mcp = FastMCP("graph-bio")
mcp.settings.streamable_http_path = "/"

# -------------------- Helpers --------------------

def _sparql_str(s: str) -> str:
    """Minimal escape for embedding Python strings as SPARQL string literals."""
    # Use triple-quoted binding in SPARQL; escape backslashes and quotes.
    return s.replace("\\", "\\\\").replace('"', '\\"')

async def _post_sparql(endpoint: str, query: str, timeout: float = 60.0) -> Dict[str, Any]:
    headers = {
        "Accept": "application/sparql-results+json",
        "User-Agent": UA,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(endpoint, data={"query": query}, headers=headers)
        if r.status_code >= 400:
            return {"error": {"status_code": r.status_code, "body": r.text[:2000]}}
        # For SELECT/ASK, endpoints return JSON; for CONSTRUCT they may return RDF,
        # but we only use SELECT queries in this server.
        return r.json()

# -------------------- Raw SPARQL tools --------------------

@mcp.tool()
async def execute_sparql_uniprot(query_string: str, format: str = "json") -> Dict[str, Any]:
    """
    Run a SPARQL query against the UniProt endpoint.
    Returns JSON for SELECT/ASK queries.
    """
    return await _post_sparql(UNIPROT, query_string, timeout=60.0)

@mcp.tool()
async def execute_sparql_rhea(query_string: str, format: str = "json") -> Dict[str, Any]:
    """
    Run a SPARQL query against the Rhea endpoint.
    Returns JSON for SELECT/ASK queries.
    """
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
  BIND(COALESCE(?fn, ?rl, ?acc) AS ?label)
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
    data = await execute_sparql_uniprot(q)
    out: List[Dict[str, Any]] = []
    for b in data.get("results", {}).get("bindings", []):
        iri = b["id"]["value"]  # e.g., http://purl.uniprot.org/uniprot/P00533
        title = b.get("label", {}).get("value") or b.get("acc", {}).get("value") or iri.rsplit("/", 1)[-1]
        out.append({
            "type": "uniprot:protein",
            "id": iri,
            "title": title,
            "snippet": "UniProtKB protein",
            "url": iri,
            "source": "uniprot",
        })
    return out

async def _search_rhea_labels(needle: str, limit: int = 10) -> List[Dict[str, Any]]:
    q = RHEA_LABEL_SEARCH % (_sparql_str(needle), limit)
    data = await execute_sparql_rhea(q)
    out: List[Dict[str, Any]] = []
    for b in data.get("results", {}).get("bindings", []):
        iri = b["id"]["value"]  # e.g., http://rdf.rhea-db.org/12345
        acc = b.get("acc", {}).get("value", "")
        eq  = b.get("eq", {}).get("value", "")
        title = f"{acc} — {eq[:160]}"
        out.append({
            "type": "rhea:reaction",
            "id": iri,
            "title": title,
            "snippet": "Rhea reaction",
            "url": iri,
            "source": "rhea",
        })
    return out

# -------------------- Search / Fetch / Choose --------------------

@mcp.tool(name="search", description="Search UniProt and/or Rhea by label, mnemonic, or ID.")
async def search(query: str, limit: int = 10, language: str = "en", source: str = "both"):
    """
    source: 'uniprot' | 'rhea' | 'both' | 'all'
    """
    results: List[Dict[str, Any]] = []
    src = (source or "both").lower()

    if src in ("uniprot", "both", "all"):
        results += await _search_uniprot_labels(query, limit=limit)
    if src in ("rhea", "both", "all"):
        results += await _search_rhea_labels(query, limit=limit)

    # de-dup by (title, source)
    seen = set()
    dedup: List[Dict[str, Any]] = []
    for r in results:
        key = (r.get("title"), r.get("source"))
        if key not in seen:
            seen.add(key)
            dedup.append(r)
    return {"results": dedup[:limit]}

@mcp.tool(name="fetch", description="Fetch content by URL, UniProt accession, or RHEA:<id>.")
async def fetch(id: str, language: str = "en"):
    """
    Accepts:
      - Full URL (http/https)
      - UniProt accession (e.g., P00533 or up to 10-char new accessions)
      - Rhea accession in the form RHEA:<digits>
    """
    # URL passthrough
    if re.match(r"^https?://", id, re.IGNORECASE):
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(id, headers={"User-Agent": UA})
            return {
                "id": id,
                "url": id,
                "mime": r.headers.get("content-type"),
                "content": r.text[:200000],
            }

    # Rhea: RHEA:12345
    m = re.match(r"^RHEA:(\d+)$", id.strip(), re.IGNORECASE)
    if m:
        iri = f"http://rdf.rhea-db.org/{m.group(1)}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(iri, headers={"User-Agent": UA})
            return {
                "id": id,
                "url": iri,
                "mime": r.headers.get("content-type"),
                "content": r.text[:200000],
            }

    # UniProt accessions: 6–10 uppercase alphanumeric (simple heuristic)
    if re.match(r"^[A-Z0-9]{6,10}$", id.strip()):
        iri = f"http://purl.uniprot.org/uniprot/{id.strip()}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(iri, headers={"User-Agent": UA})
            return {
                "id": id,
                "url": iri,
                "mime": r.headers.get("content-type"),
                "content": r.text[:200000],
            }

    return {"error": "Pass a URL, a UniProt accession (e.g., P00533), or a Rhea ID like RHEA:12345."}

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
    # Default to UniProt if unclear, since proteins are often the entry point.
    return {"target": "uniprot", "reason": "default fallback"}

# -------------------- ASGI app --------------------

app = mcp.streamable_http_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
