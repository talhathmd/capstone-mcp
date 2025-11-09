# MCP-Mediated Knowledge Graph Retrieval for LLMs

## Overview

LLMs are powerful communicators, yet they remain unreliable on relation-dense facts that require joins, role-specific participants, or directionality. Conventional RAG improves recall but keeps knowledge in text form; as a result, constraints and provenance are not first-class. RDF knowledge graphs explicitly encode entities and typed edges, while SPARQL provides a declarative language for precise and auditable queries.

We investigate a standards-based integration in which the LLM discovers and invokes a SPARQL tool through the Model Context Protocol (MCP) during reasoning. Our system exposes a minimal SPARQL tool plus schema/prefix resources; questions are translated to SPARQL with pattern-guided prompts; results (bindings) are fed back to the model, which must ground its answer in those bindings. Using a domain knowledge graph as a case study, we ask: Does MCP-mediated KG retrieval improve accuracy and attribution on relation-focused questions? We report where it helps most, where it breaks (endpoint errors, query generation, ambiguity), and the operational guardrails that make public endpoints usable in practice.

## How It Works

This MCP server exposes SPARQL query tools to an LLM (like ChatGPT). The LLM translates natural language questions into SPARQL queries, executes them against the Rhea biochemical reaction database, and uses the structured results to provide accurate, attributed answers.

The workflow:
1. User asks a question in natural language
2. LLM (ChatGPT) translates the question to SPARQL using the available tools
3. SPARQL query is executed against the Rhea endpoint
4. Results (JSON bindings) are returned to the LLM
5. LLM grounds its answer in the actual query results

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

The server provides several tools for querying the Rhea knowledge graph:

- **`execute_sparql_rhea`** - Execute raw SPARQL queries (main tool for LLM to use)
- **`reactions_producing_product_from_substrate_names`** - Find reactions by substrate/product names
- **`reactions_by_ec`** - Find reactions by EC number
- **`find_reaction_by_equation_text`** - Search reactions by equation text
- **`children_of_reaction`** - Get child reactions of a parent
- **`fetch`** - Fetch raw RDF content for a Rhea accession or URL
- **`debug_ping`** - Test connection to the endpoint

## Configuration

Environment variables:
- `RHEA_SPARQL` - SPARQL endpoint URL (default: `https://sparql.rhea-db.org/sparql`)
- `BIO_UA` - User-Agent string for HTTP requests
- `BIO_HTTP2` - Enable HTTP/2 (`auto`, `on`, `off`)
- `PORT` - Server port (default: `8080`)

## Notes

- The server handles various SPARQL endpoint quirks by trying POST/GET with and without format parameters
- Results are limited to prevent runaway queries (max 2000 results)
- Only SELECT and ASK queries are supported (CONSTRUCT/DESCRIBE are rejected)
