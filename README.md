# MCP Integration: Wikidata KG-RAG


This adds a **real MCP server** that exposes three tools:


- `generate_sparql(question, limit)` → `{ sparql }`
- `query_wikidata(sparql, limit)` → `{ head, rows, json }`
- `answer(question, sparql, data)` → `{ answer }`


## Install
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env # paste your OpenAI key
```


## Run the MCP server directly (stdio)
```bash
OPENAI_API_KEY=sk-... python mcp_server/wikidata_server.py
# (it runs in stdio; typically you connect via a client)
```


### Option A — Use the included terminal client
```bash
# one-shot
python mcp_client_cli.py "Who is the spouse of Barack Obama?"


# REPL
python mcp_client_cli.py --repl
```


### Option B — Inspect with MCP dev tools (optional)
```bash
# Requires the MCP CLI (already installed by mcp[cli]) and uv is optional
mcp dev mcp_server/wikidata_server.py
```


## Notes
- The server uses your existing OpenAI model to generate SPARQL and to compose answers.
- We defensively normalize the SPARQL so `SERVICE wikibase:label` is inside `WHERE` and apply a sane `LIMIT`.
- You can swap in an HTTP transport later via `mcp.run(transport="streamable-http")` if needed.