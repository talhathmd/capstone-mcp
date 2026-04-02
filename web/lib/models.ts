export type ProviderId = "openai" | "anthropic";

export type KnowledgeBackend = "wikidata" | "rhea";

export const OPENAI_MODELS = [
  { id: "gpt-4o", label: "GPT-4o" },
  { id: "gpt-4o-mini", label: "GPT-4o mini" },
  { id: "gpt-4-turbo", label: "GPT-4 Turbo" },
] as const;

export const ANTHROPIC_MODELS = [
  { id: "claude-sonnet-4-6", label: "Claude Sonnet 4.6" },
  { id: "claude-opus-4-6", label: "Claude Opus 4.6" },
  { id: "claude-haiku-4-5-20251001", label: "Claude Haiku 4.5" },
] as const;

export function defaultModel(provider: ProviderId): string {
  return provider === "openai" ? OPENAI_MODELS[1].id : ANTHROPIC_MODELS[0].id;
}

export function isAllowedModel(provider: ProviderId, model: string): boolean {
  const list = provider === "openai" ? OPENAI_MODELS : ANTHROPIC_MODELS;
  return list.some((m) => m.id === model);
}
