export type ProviderId = "openai" | "anthropic";

export type KnowledgeBackend = "wikidata" | "rhea";

export const OPENAI_MODELS = [
  { id: "gpt-4o", label: "GPT-4o" },
  { id: "gpt-4o-mini", label: "GPT-4o mini" },
  { id: "gpt-4-turbo", label: "GPT-4 Turbo" },
] as const;

export const ANTHROPIC_MODELS = [
  { id: "claude-sonnet-4-20250514", label: "Claude Sonnet 4" },
  { id: "claude-3-5-sonnet-20241022", label: "Claude 3.5 Sonnet" },
  { id: "claude-3-5-haiku-20241022", label: "Claude 3.5 Haiku" },
] as const;

export function defaultModel(provider: ProviderId): string {
  return provider === "openai" ? OPENAI_MODELS[1].id : ANTHROPIC_MODELS[1].id;
}

export function isAllowedModel(provider: ProviderId, model: string): boolean {
  const list = provider === "openai" ? OPENAI_MODELS : ANTHROPIC_MODELS;
  return list.some((m) => m.id === model);
}
