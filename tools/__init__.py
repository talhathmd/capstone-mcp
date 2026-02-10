"""
Tools package — MCP tool modules for different knowledge graph backends.

Each module exposes a `register(mcp)` function that attaches its tools
to the MCP server instance.

Modules:
  shared.py     — HTTP client, SPARQL execution, caching, linting, error codes
  rhea.py       — Rhea biochemical reaction database tools
  wikidata.py   — Wikidata grounding + SPARQL execution tools
"""
