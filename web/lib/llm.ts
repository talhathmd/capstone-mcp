import { createAnthropic } from "@ai-sdk/anthropic";
import { openai } from "@ai-sdk/openai";
import type { ProviderId } from "@/lib/models";

/** Resolve key from env (supports common ANTHORPIC typo). */
export function anthropicApiKey(): string | undefined {
  const k =
    process.env.ANTHROPIC_API_KEY?.trim() ||
    process.env.ANTHORPIC_API_KEY?.trim();
  return k || undefined;
}

export function hasAnthropicApiKey(): boolean {
  return Boolean(anthropicApiKey());
}

export function getLanguageModel(provider: ProviderId, modelId: string) {
  if (provider === "openai") {
    return openai(modelId);
  }
  return createAnthropic({
    apiKey: anthropicApiKey() ?? "",
  })(modelId);
}
