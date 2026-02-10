# MCP-Mediated Knowledge Graph Retrieval for LLMs

## Overview

LLMs are powerful communicators, yet they remain unreliable on relation-dense facts that require joins, role-specific participants, or directionality. Conventional RAG improves recall but keeps knowledge in text form; as a result, constraints and provenance are not first-class. RDF knowledge graphs explicitly encode entities and typed edges, while SPARQL provides a declarative language for precise and auditable queries.

We investigate a standards-based integration in which the LLM discovers and invokes a SPARQL tool through the Model Context Protocol (MCP) during reasoning. Our system exposes a minimal SPARQL tool plus schema/prefix resources; questions are translated to SPARQL with pattern-guided prompts; results (bindings) are fed back to the model, which must ground its answer in those bindings. Using a domain knowledge graph as a case study, we ask: Does MCP-mediated KG retrieval improve accuracy and attribution on relation-focused questions? We report where it helps most, where it breaks (endpoint errors, query generation, ambiguity), and the operational guardrails that make public endpoints usable in practice.

## Architecture

```
server.py             — Entry point, MCP app, ASGI routing
tools/
  __init__.py          — Package docstring
  shared.py            — HTTP client, SPARQL execution, caching, linting, error codes
  rhea.py              — Rhea biochemical reaction tools
  wikidata.py          — Wikidata grounding + SPARQL execution tools
```

## How It Works

This MCP server exposes SPARQL query tools to an LLM (like ChatGPT). It supports two knowledge graph backends:

### Rhea (biochemistry)
- Pre-built query tools for common reaction lookups
- Freeform SPARQL tool with schema contract in the description

### Wikidata (general KG)
- **Grounding-first workflow**: the LLM must call `search_entity` / `search_property` to discover real QIDs and PIDs before writing SPARQL — prevents hallucinated IDs
- **Safety linter**: checks every query for LIMIT, blocked constructs (FROM/GRAPH/unbounded paths), and validates that all IDs came from grounding tools
- **Dry-run**: executes with LIMIT 1 first to catch syntax errors fast
- **Structured errors**: every failure returns a machine-readable error code (SYNTAX, TIMEOUT, RATE_LIMIT, etc.) and a repair hint
- **TTL caching**: search results and successful queries are cached in-memory to avoid repeat calls
- **Rate-limit awareness**: self-throttles WDQS requests with exponential backoff on 429s

### Workflow

1. User asks a question in natural language
2. LLM calls `search_entity` / `search_property` to ground IDs
3. LLM writes SPARQL using only grounded IDs
4. `run_sparql_wikidata` lints → dry-runs → executes the query
5. Structured results (or error + hint) returned to the LLM
6. LLM grounds its answer in the actual query results

## Installation

```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Running the Server

```bash
python server.py
```

The server runs on port 8080 by default (set `PORT` env var to change). It exposes an MCP endpoint at the root path `/` that can be connected to via the Model Context Protocol.

## Available Tools

### Wikidata — Grounding

| Tool | Description |
|------|-------------|
| `search_entity` | Find QIDs by text (e.g. "Albert Einstein" → Q937) |
| `search_property` | Find PIDs by text (e.g. "instance of" → P31) |
| `get_schema_context` | Fetch labels, descriptions, datatypes for known IDs |

### Wikidata — Execution

| Tool | Description |
|------|-------------|
| `run_sparql_wikidata` | Lint → dry-run → execute SPARQL against WDQS |
| `normalize_sparql_error` | Classify error message into stable code + hint |
| `debug_ping_wikidata` | Test WDQS connectivity |

### Rhea — Queries

| Tool | Description |
|------|-------------|
| `execute_sparql_rhea` | Freeform SPARQL against Rhea endpoint |
| `reactions_producing_product_from_substrate_names` | Find reactions by substrate → product names |
| `reactions_by_ec` | Find reactions by EC number |
| `find_reaction_by_equation_text` | Search reactions by equation text |
| `children_of_reaction` | Get child reactions of a parent |

### Utility

| Tool | Description |
|------|-------------|
| `fetch` | Fetch raw content for a Rhea accession or URL |
| `debug_ping` | Test Rhea endpoint + HTTP/2 status |

## Configuration

Environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `RHEA_SPARQL` | `https://sparql.rhea-db.org/sparql` | Rhea SPARQL endpoint |
| `BIO_UA` | `GraphBio/3.0 (contact: ...)` | User-Agent for HTTP requests |
| `BIO_HTTP2` | `auto` | HTTP/2 toggle: `auto`, `on`, `off` |
| `PORT` | `8080` | Server listen port |

## Reliability Features

- **LIMIT enforcement**: injected if missing, capped if too high
- **Blocked constructs**: FROM, FROM NAMED, GRAPH, unbounded property paths
- **SERVICE allow-list**: only `SERVICE wikibase:label` is permitted
- **ID validation**: SPARQL entities/properties must come from grounding tool output
- **Dry-run**: LIMIT 1 execution catches syntax errors before the real query
- **Error normalization**: raw endpoint errors mapped to SYNTAX / TIMEOUT / RATE_LIMIT / ENDPOINT_ERROR / UNKNOWN
- **Exponential backoff**: automatic throttle on WDQS 429 responses
- **In-memory caching**: entity/property search (10 min), schema (15 min), query results (5 min)
- **POST/GET fallback**: tries 4 HTTP methods per query to handle endpoint quirks

## Notes

- Only SELECT and ASK queries are supported (CONSTRUCT/DESCRIBE are rejected)
- WDQS rate-limits aggressively; the server self-throttles to stay under limits
- Results capped at 500 rows for Wikidata, 2000 for Rhea
