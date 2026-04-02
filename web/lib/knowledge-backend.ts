import type { ToolSet } from "ai";
import type { KnowledgeBackend } from "@/lib/models";

const WIKIDATA = new Set([
  "search_entity",
  "search_property",
  "get_schema_context",
  "run_sparql_wikidata",
  "normalize_sparql_error",
  "debug_ping_wikidata",
]);

const RHEA = new Set([
  "reactions_producing_product_from_substrate_names",
  "reactions_by_ec",
  "find_reaction_by_equation_text",
  "children_of_reaction",
  "execute_sparql_rhea",
  "fetch",
  "debug_ping",
]);

export function toolNameAllowed(name: string, backend: KnowledgeBackend): boolean {
  if (backend === "wikidata") return WIKIDATA.has(name);
  return RHEA.has(name);
}

/** Narrow MCP tool map to one knowledge graph. */
export function filterToolKeys(
  tools: ToolSet,
  backend: KnowledgeBackend,
): ToolSet {
  const out: ToolSet = {};
  for (const key of Object.keys(tools)) {
    if (toolNameAllowed(key, backend)) {
      const t = tools[key];
      if (t) out[key] = t;
    }
  }
  return out;
}
