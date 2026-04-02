import { createMCPClient } from "@ai-sdk/mcp";
import { generateText, stepCountIs, type ToolSet } from "ai";
import { cookies } from "next/headers";
import { NextResponse } from "next/server";
import { z } from "zod";
import { AUTH_COOKIE_NAME, verifySessionCookie } from "@/lib/auth";
import { filterToolKeys } from "@/lib/knowledge-backend";
import { getLanguageModel, hasAnthropicApiKey } from "@/lib/llm";
import type { KnowledgeBackend, ProviderId } from "@/lib/models";
import { isAllowedModel } from "@/lib/models";
import { BASE_SYSTEM, mcpSystemPrompt } from "@/lib/prompts";

export const runtime = "nodejs";
export const maxDuration = 300;

const bodySchema = z.object({
  question: z.string().trim().min(1).max(8000),
  provider: z.enum(["openai", "anthropic"]),
  model: z.string().min(1),
  knowledgeBackend: z.enum(["wikidata", "rhea"]),
});

function envInt(name: string, fallback: number): number {
  const v = process.env[name];
  if (!v) return fallback;
  const n = parseInt(v, 10);
  return Number.isFinite(n) ? n : fallback;
}

async function requireSession(): Promise<Response | null> {
  const store = await cookies();
  const token = store.get(AUTH_COOKIE_NAME)?.value;
  const authSecret = process.env.AUTH_SECRET;
  const password = process.env.COMPARE_PASSWORD;
  if (!authSecret || !password) {
    return NextResponse.json(
      { error: "Server missing AUTH_SECRET or COMPARE_PASSWORD" },
      { status: 500 },
    );
  }
  if (!(await verifySessionCookie(token, authSecret, password))) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  return null;
}

function providerKeyOk(provider: ProviderId): boolean {
  if (provider === "openai") return Boolean(process.env.OPENAI_API_KEY?.trim());
  return hasAnthropicApiKey();
}

type LaneResult = {
  text: string;
  error: string | null;
  usage?: { inputTokens?: number; outputTokens?: number; totalTokens?: number };
  finishReason?: string;
  durationMs: number;
  toolSteps?: number;
};

export async function POST(request: Request) {
  const unauthorized = await requireSession();
  if (unauthorized) return unauthorized;

  let json: unknown;
  try {
    json = await request.json();
  } catch {
    return NextResponse.json({ error: "Invalid JSON" }, { status: 400 });
  }

  const parsed = bodySchema.safeParse(json);
  if (!parsed.success) {
    return NextResponse.json(
      { error: "Invalid body", details: parsed.error.flatten() },
      { status: 400 },
    );
  }

  const { question, provider, model, knowledgeBackend } = parsed.data;

  if (!isAllowedModel(provider, model)) {
    return NextResponse.json({ error: "Model not allowed" }, { status: 400 });
  }

  if (!providerKeyOk(provider)) {
    return NextResponse.json(
      {
        error:
          provider === "openai"
            ? "Missing OPENAI_API_KEY on the server"
            : "Missing ANTHROPIC_API_KEY for the Next.js app (Vercel env or web/.env.local). The MCP Python server does not use this key.",
      },
      { status: 503 },
    );
  }

  const mcpUrl = process.env.MCP_SERVER_URL?.trim();
  if (!mcpUrl) {
    return NextResponse.json(
      { error: "Missing MCP_SERVER_URL (your Python MCP base URL, e.g. https://host:8080)" },
      { status: 503 },
    );
  }

  const maxOutputTokens = envInt("COMPARE_MAX_OUTPUT_TOKENS", 2048);
  const maxToolSteps = envInt("COMPARE_MAX_TOOL_STEPS", 12);
  const timeoutMs = envInt("COMPARE_TIMEOUT_MS", 180_000);

  const languageModel = getLanguageModel(provider, model);

  const logPayload = {
    event: "compare",
    provider,
    model,
    knowledgeBackend,
    questionLength: question.length,
  };
  console.log(JSON.stringify(logPayload));

  const baselinePromise = (async (): Promise<LaneResult> => {
    const t0 = Date.now();
    try {
      const result = await generateText({
        model: languageModel,
        system: BASE_SYSTEM,
        prompt: question,
        maxOutputTokens,
        temperature: 0.2,
        timeout: timeoutMs,
      });
      console.log(
        JSON.stringify({
          ...logPayload,
          lane: "baseline",
          usage: result.totalUsage,
          durationMs: Date.now() - t0,
        }),
      );
      return {
        text: result.text,
        error: null,
        usage: {
          inputTokens: result.totalUsage.inputTokens,
          outputTokens: result.totalUsage.outputTokens,
          totalTokens: result.totalUsage.totalTokens,
        },
        finishReason: result.finishReason,
        durationMs: Date.now() - t0,
      };
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      console.error(JSON.stringify({ ...logPayload, lane: "baseline", error: msg }));
      return {
        text: "",
        error: msg,
        durationMs: Date.now() - t0,
      };
    }
  })();

  const mcpPromise = (async (): Promise<LaneResult> => {
    const t0 = Date.now();
    let client: Awaited<ReturnType<typeof createMCPClient>> | undefined;
    try {
      client = await createMCPClient({
        transport: {
          type: "http",
          url: mcpUrl,
        },
      });

      const rawTools = await client.tools();
      const tools = filterToolKeys(
        rawTools as ToolSet,
        knowledgeBackend as KnowledgeBackend,
      );

      if (Object.keys(tools).length === 0) {
        throw new Error(
          "No MCP tools left after backend filter. Check MCP_SERVER_URL and knowledge backend selection.",
        );
      }

      const result = await generateText({
        model: languageModel,
        system: mcpSystemPrompt(knowledgeBackend as KnowledgeBackend),
        prompt: question,
        tools,
        stopWhen: stepCountIs(maxToolSteps),
        maxOutputTokens,
        temperature: 0.2,
        timeout: timeoutMs,
      });

      console.log(
        JSON.stringify({
          ...logPayload,
          lane: "mcp",
          usage: result.totalUsage,
          toolSteps: result.steps?.length ?? 0,
          durationMs: Date.now() - t0,
        }),
      );

      return {
        text: result.text,
        error: null,
        usage: {
          inputTokens: result.totalUsage.inputTokens,
          outputTokens: result.totalUsage.outputTokens,
          totalTokens: result.totalUsage.totalTokens,
        },
        finishReason: result.finishReason,
        durationMs: Date.now() - t0,
        toolSteps: result.steps?.length,
      };
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      console.error(JSON.stringify({ ...logPayload, lane: "mcp", error: msg }));
      return {
        text: "",
        error: msg,
        durationMs: Date.now() - t0,
      };
    } finally {
      if (client) {
        try {
          await client.close();
        } catch {
          /* ignore */
        }
      }
    }
  })();

  const [baseline, mcp] = await Promise.all([baselinePromise, mcpPromise]);

  return NextResponse.json({ baseline, mcp });
}
