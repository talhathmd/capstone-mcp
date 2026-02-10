"""
Rhea-specific MCP tools.

Exposes pre-built query helpers and a freeform SPARQL tool for the
Rhea biochemical reaction database (https://www.rhea-db.org/).

All tools are registered via the `register(mcp)` function so that
server.py can attach them to the FastMCP instance.
"""

import os
import re
from typing import Any, Dict, Optional

import httpx

from .shared import (
    UA,
    H2_AVAILABLE,
    http2_enabled,
    exec_sparql_json,
    escape_sparql_string,
    clamp_limit,
)

# Rhea SPARQL endpoint (overridable via env var)
RHEA_SPARQL = os.getenv("RHEA_SPARQL", "https://sparql.rhea-db.org/sparql")


def register(mcp) -> None:
    """Attach every Rhea tool to the given MCP server."""

    # ------------------------------------------------------------------
    # Pre-built query tools (safe, parameterized SPARQL)
    # ------------------------------------------------------------------

    @mcp.tool(
        name="reactions_producing_product_from_substrate_names",
        description=(
            "Find APPROVED Rhea reactions that convert a given substrate "
            "name to a given product name.\n"
            "Matches by compound names (case-insensitive, contains).\n"
            "Directionality is enforced via rh:transformableTo (left→right).\n\n"
            "Args:\n"
            "  substrate_name: e.g. \"L-glutamine\"\n"
            "  product_name:   e.g. \"ammonia\"\n"
            "  limit:          optional integer (default 200)\n"
            "Returns: ?reaction IRI and ?equation string."
        ),
    )
    async def reactions_producing_product_from_substrate_names(
        substrate_name: str,
        product_name: str,
        limit: Optional[int] = None,
    ):
        s_name = escape_sparql_string(substrate_name or "")
        p_name = escape_sparql_string(product_name or "")
        lim = clamp_limit(limit, 200)
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
        return await exec_sparql_json(RHEA_SPARQL, q)

    # ------------------------------------------------------------------

    @mcp.tool(
        name="reactions_by_ec",
        description=(
            "Find APPROVED Rhea reactions for a given EC number.\n"
            "Args:\n"
            "  ec_number: string like '1.11.1.6'\n"
            "  limit: optional integer (default 200)\n"
            "Returns: ?reaction and ?equation."
        ),
    )
    async def reactions_by_ec(ec_number: str, limit: Optional[int] = None):
        num = (ec_number or "").strip()
        lim = clamp_limit(limit, 200)
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
        return await exec_sparql_json(RHEA_SPARQL, q)

    # ------------------------------------------------------------------

    @mcp.tool(
        name="find_reaction_by_equation_text",
        description=(
            "Search reactions by equation text (case-insensitive substring "
            "match on rh:equation).\n"
            "Args:\n"
            "  contains_text: e.g. 'alcohol + NAD+' or '2 H2O2'\n"
            "  limit: optional integer (default 50)\n"
            "Returns: ?reaction, ?accession, ?equation."
        ),
    )
    async def find_reaction_by_equation_text(
        contains_text: str,
        limit: Optional[int] = None,
    ):
        text = escape_sparql_string(contains_text or "")
        lim = clamp_limit(limit, 50)
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
        return await exec_sparql_json(RHEA_SPARQL, q)

    # ------------------------------------------------------------------

    @mcp.tool(
        name="children_of_reaction",
        description=(
            "Fetch specific child reactions of a given parent reaction "
            "(by RHEA:<digits>), following rdfs:subClassOf+.\n"
            "Args:\n"
            "  parent_rhea_id: e.g. 'RHEA:12345'\n"
            "  limit: optional integer (default 500)\n"
            "Returns: ?child and ?childEq."
        ),
    )
    async def children_of_reaction(
        parent_rhea_id: str,
        limit: Optional[int] = None,
    ):
        lim = clamp_limit(limit, 500)
        m = re.match(
            r"^RHEA:(\d+)$", (parent_rhea_id or "").strip(), re.IGNORECASE
        )
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
        return await exec_sparql_json(RHEA_SPARQL, q)

    # ------------------------------------------------------------------
    # Freeform SPARQL tool (the LLM writes its own query)
    # ------------------------------------------------------------------

    @mcp.tool(
        name="execute_sparql_rhea",
        description=(
            "Run a SELECT/ASK SPARQL query against the Rhea endpoint and "
            "return JSON only.  YOU (the AI) must translate NL → SPARQL.\n"
            "Contract:\n"
            "• Prefixes to use: "
            "  PREFIX rh:   <http://rdf.rhea-db.org/>  "
            "  PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>  "
            "  PREFIX ec:   <http://purl.uniprot.org/enzyme/>  "
            "  PREFIX CHEBI: <http://purl.obolibrary.org/obo/CHEBI_>  "
            "  PREFIX pubmed: <http://rdf.ncbi.nlm.nih.gov/pubmed/>\n"
            "• Reactions: ?r rdfs:subClassOf rh:Reaction .  "
            "  IDs/text: ?r rh:accession ?acc ; rh:equation ?eq .  "
            "  Optional: ?r rh:status rh:Approved ; "
            "rh:isTransport ?isTransport .\n"
            "• EC mapping: ?r rh:ec ?ec .\n"
            "• Participants: ?r rh:side ?s . ?s rh:contains ?part . "
            "  ?part rh:compound ?c . "
            "  Compound binding (any of): ?c (rh:chebi | rh:underlyingChebi "
            "| (rh:reactivePart/rh:chebi)) ?chebi . "
            "  Names: ?c rh:name ?name .\n"
            "• Directionality: ?left rh:transformableTo ?right . "
            "  left = substrates, right = products.\n"
            "• Cross-refs: rdfs:seeAlso on the reaction / "
            "rh:directionalReaction / rh:bidirectionalReaction.\n"
            "• Citations: ?r rh:citation ?pm .\n"
            "• Descendants: ?child rdfs:subClassOf+ ?parent .\n"
            "• CHEBI class: rdfs:label → rdfs:subClassOf+ expansion.\n"
            "• Always include a LIMIT and escape double quotes."
        ),
    )
    async def execute_sparql_rhea(
        query_string: str,
        timeout: float = 60.0,
    ) -> Dict[str, Any]:
        s = (query_string or "").strip()
        if not s:
            return {"error": "Empty SPARQL query."}
        if re.search(r"\b(CONSTRUCT|DESCRIBE)\b", s, flags=re.IGNORECASE):
            return {"error": "Use SELECT or ASK for JSON results."}
        return await exec_sparql_json(RHEA_SPARQL, s, timeout=timeout)

    # ------------------------------------------------------------------
    # Utility tools
    # ------------------------------------------------------------------

    @mcp.tool(
        name="fetch",
        description=(
            "Fetch raw content for a Rhea accession (RHEA:<digits>) "
            "or an HTTP(S) URL."
        ),
    )
    async def fetch(id: str, language: str = "en"):
        s = (id or "").strip()
        use_h2 = http2_enabled()

        # Plain URL
        if re.match(r"^https?://", s, re.IGNORECASE):
            try:
                async with httpx.AsyncClient(
                    timeout=30.0, follow_redirects=True, http2=use_h2
                ) as client:
                    r = await client.get(s, headers={"User-Agent": UA})
                return {
                    "id": s,
                    "url": s,
                    "mime": r.headers.get("content-type"),
                    "content": r.text[:200_000],
                }
            except Exception as e:
                return {"error": f"Fetch failed for URL: {e}"}

        # Rhea accession → RDF IRI
        m = re.match(r"^RHEA:(\d+)$", s, re.IGNORECASE)
        if m:
            iri = f"https://rdf.rhea-db.org/{m.group(1)}"
            try:
                async with httpx.AsyncClient(
                    timeout=30.0, follow_redirects=True, http2=use_h2
                ) as client:
                    r = await client.get(iri, headers={"User-Agent": UA})
                return {
                    "id": s,
                    "url": iri,
                    "mime": r.headers.get("content-type"),
                    "content": r.text[:200_000],
                }
            except Exception as e:
                return {"error": f"Fetch failed for Rhea accession: {e}"}

        return {"error": "Provide a URL or a Rhea accession like RHEA:12345."}

    # ------------------------------------------------------------------

    @mcp.tool(
        name="debug_ping",
        description="Test connection to Rhea endpoint and show HTTP/2 status.",
    )
    async def debug_ping():
        use_h2 = http2_enabled()
        rh = await exec_sparql_json(
            RHEA_SPARQL, "SELECT (1 AS ?x) WHERE {}", timeout=10.0
        )
        return {
            "rhea": rh,
            "http2_enabled": use_h2,
            "h2_installed": H2_AVAILABLE,
        }
