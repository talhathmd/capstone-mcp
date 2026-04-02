"use client";

import { AnimatePresence, motion } from "framer-motion";
import {
  ArrowLeftRight,
  Dna,
  Globe2,
  LogOut,
  Sparkles,
  Zap,
} from "lucide-react";
import { useMemo, useState } from "react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { AssistantMarkdown } from "@/components/assistant-markdown";
import { cn } from "@/lib/utils";
import {
  ANTHROPIC_MODELS,
  OPENAI_MODELS,
  defaultModel,
  type KnowledgeBackend,
  type ProviderId,
} from "@/lib/models";

type Lane = {
  text: string;
  error: string | null;
  usage?: {
    inputTokens?: number;
    outputTokens?: number;
    totalTokens?: number;
  };
  finishReason?: string;
  durationMs: number;
  toolSteps?: number;
};

type CompareResponse = { baseline: Lane; mcp: Lane };

const fadeUp = {
  initial: { opacity: 0, y: 12 },
  animate: { opacity: 1, y: 0 },
  exit: { opacity: 0, y: -8 },
};

export function CompareConsole() {
  const [provider, setProvider] = useState<ProviderId>("openai");
  const [model, setModel] = useState(() => defaultModel("openai"));
  const [knowledgeBackend, setKnowledgeBackend] =
    useState<KnowledgeBackend>("wikidata");
  const [question, setQuestion] = useState("");
  const [pending, setPending] = useState(false);
  const [result, setResult] = useState<CompareResponse | null>(null);
  const [clientError, setClientError] = useState<string | null>(null);

  const models = useMemo(
    () => (provider === "openai" ? OPENAI_MODELS : ANTHROPIC_MODELS),
    [provider],
  );

  function onProviderChange(v: ProviderId) {
    setProvider(v);
    setModel(defaultModel(v));
  }

  async function runCompare() {
    setClientError(null);
    setPending(true);
    try {
      const res = await fetch("/api/compare", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          question,
          provider,
          model,
          knowledgeBackend,
        }),
      });
      const data = (await res.json()) as CompareResponse & { error?: string };
      if (!res.ok) {
        setClientError(data.error ?? `Request failed (${res.status})`);
        setResult(null);
        return;
      }
      setResult(data);
    } catch (e) {
      setClientError(e instanceof Error ? e.message : "Request failed");
      setResult(null);
    } finally {
      setPending(false);
    }
  }

  async function logout() {
    await fetch("/api/auth/logout", { method: "POST" });
    window.location.href = "/login";
  }

  return (
    <div className="flex min-h-screen flex-col bg-[radial-gradient(ellipse_120%_80%_at_50%_-20%,rgba(139,92,246,0.12),transparent)]">
      <header className="sticky top-0 z-30 border-b border-border/50 bg-background/75 backdrop-blur-xl">
        <div className="mx-auto flex max-w-5xl items-center justify-between gap-4 px-4 py-3 sm:px-6">
          <div className="flex items-center gap-3">
            <motion.div
              className="flex size-9 items-center justify-center rounded-xl bg-gradient-to-br from-violet-600 to-indigo-600 text-white shadow-lg shadow-violet-500/20"
              whileHover={{ scale: 1.03 }}
              transition={{ type: "spring", stiffness: 400, damping: 24 }}
            >
              <Zap className="size-[18px]" aria-hidden />
            </motion.div>
            <div>
              <h1 className="text-[15px] font-semibold tracking-tight">
                KG Compare Lab
              </h1>
              <p className="text-xs text-muted-foreground">
                Same model · one question · two retrieval modes
              </p>
            </div>
          </div>
          <Button variant="ghost" size="sm" onClick={() => void logout()}>
            <LogOut className="mr-1.5 size-4" />
            Sign out
          </Button>
        </div>

        {/* Controls toolbar — top */}
        <div className="border-t border-border/40 bg-muted/30">
          <div className="mx-auto flex max-w-5xl flex-col gap-3 px-4 py-3 sm:flex-row sm:flex-wrap sm:items-end sm:justify-between sm:gap-4 sm:px-6">
            <div className="flex flex-1 flex-col gap-3 sm:flex-row sm:items-end sm:gap-3">
              <div className="grid min-w-0 flex-1 grid-cols-1 gap-3 sm:grid-cols-2 sm:gap-3 lg:max-w-xl">
                <div className="space-y-1.5">
                  <Label className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                    Provider
                  </Label>
                  <Select
                    value={provider}
                    onValueChange={(v) => onProviderChange(v as ProviderId)}
                  >
                    <SelectTrigger className="h-9 w-full bg-background/80">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="openai">OpenAI</SelectItem>
                      <SelectItem value="anthropic">Anthropic</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-1.5">
                  <Label className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                    Model
                  </Label>
                  <Select
                    value={model}
                    onValueChange={(v) => {
                      if (v) setModel(v);
                    }}
                  >
                    <SelectTrigger className="h-9 w-full bg-background/80">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {models.map((m) => (
                        <SelectItem key={m.id} value={m.id}>
                          {m.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              </div>

              <div className="space-y-1.5 sm:min-w-[220px]">
                <Label className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                  MCP knowledge graph
                </Label>
                <div
                  className="flex rounded-lg bg-background/80 p-0.5 ring-1 ring-border/60"
                  role="group"
                  aria-label="Knowledge graph for MCP tools"
                >
                  <button
                    type="button"
                    onClick={() => setKnowledgeBackend("wikidata")}
                    className={cn(
                      "flex flex-1 items-center justify-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-medium transition-all duration-200",
                      knowledgeBackend === "wikidata"
                        ? "bg-white text-foreground shadow-sm ring-1 ring-border/50"
                        : "text-muted-foreground hover:text-foreground",
                    )}
                  >
                    <Globe2 className="size-3.5 shrink-0 opacity-80" />
                    Wikidata
                  </button>
                  <button
                    type="button"
                    onClick={() => setKnowledgeBackend("rhea")}
                    className={cn(
                      "flex flex-1 items-center justify-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-medium transition-all duration-200",
                      knowledgeBackend === "rhea"
                        ? "bg-white text-foreground shadow-sm ring-1 ring-border/50"
                        : "text-muted-foreground hover:text-foreground",
                    )}
                  >
                    <Dna className="size-3.5 shrink-0 opacity-80" />
                    Rhea
                  </button>
                </div>
              </div>
            </div>
            <Badge variant="secondary" className="w-fit shrink-0 font-normal">
              Keys stay on the server
            </Badge>
          </div>
        </div>
      </header>

      {/* Results — upper main area (not chat-style) */}
      <div className="relative flex min-h-0 flex-1 flex-col">
        <div className="mx-auto w-full max-w-5xl flex-1 px-4 py-4 sm:px-6">
          <AnimatePresence mode="wait">
            {clientError ? (
              <motion.div
                key="err"
                {...fadeUp}
                transition={{ duration: 0.22 }}
              >
                <Alert variant="destructive" className="border-destructive/40">
                  <AlertTitle>Error</AlertTitle>
                  <AlertDescription>{clientError}</AlertDescription>
                </Alert>
              </motion.div>
            ) : null}

            {pending ? (
              <motion.div
                key="loading"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                className="overflow-hidden rounded-2xl border border-border/60 bg-card/40 shadow-sm backdrop-blur-sm"
              >
                <div className="grid grid-cols-2 divide-x divide-border/60">
                  {[0, 1].map((i) => (
                    <div key={i} className="space-y-3 p-5 sm:p-6">
                      <div className="h-3 w-24 animate-pulse rounded-md bg-muted" />
                      <div className="space-y-2">
                        <div className="h-2.5 w-full animate-pulse rounded bg-muted/80" />
                        <div className="h-2.5 w-[92%] animate-pulse rounded bg-muted/80" />
                        <div className="h-2.5 w-[78%] animate-pulse rounded bg-muted/80" />
                      </div>
                    </div>
                  ))}
                </div>
              </motion.div>
            ) : result ? (
              <motion.div
                key="result"
                {...fadeUp}
                transition={{ duration: 0.28, ease: [0.22, 1, 0.36, 1] }}
                className="overflow-hidden rounded-2xl border border-border/60 bg-gradient-to-b from-card to-card/80 shadow-lg shadow-violet-500/[0.06] ring-1 ring-border/40"
              >
                <div className="grid grid-cols-1 divide-y divide-border/50 md:grid-cols-2 md:divide-x md:divide-y-0">
                  <ComparisonColumn
                    label="Baseline"
                    hint="Model only — no tools"
                    icon={<Sparkles className="size-3.5 text-amber-600/90" />}
                    lane={result.baseline}
                    tone="slate"
                  />
                  <ComparisonColumn
                    label="MCP"
                    hint="Streamable HTTP · tools for selected KG"
                    icon={<Zap className="size-3.5 text-violet-600/90" />}
                    lane={result.mcp}
                    tone="violet"
                  />
                </div>
              </motion.div>
            ) : (
              <motion.div
                key="empty"
                {...fadeUp}
                transition={{ duration: 0.25 }}
                className="flex min-h-[min(40vh,320px)] flex-col items-center justify-center rounded-2xl border border-dashed border-border/60 bg-muted/15 px-6 py-12 text-center"
              >
                <p className="max-w-sm text-sm text-muted-foreground">
                  Choose settings above, enter a question below, then run a
                  comparison. Results appear here as a side-by-side readout —
                  not a chat.
                </p>
              </motion.div>
            )}
          </AnimatePresence>
        </div>

        {/* Single input — bottom, centered */}
        <motion.div
          layout
          className="sticky bottom-0 z-20 border-t border-border/50 bg-gradient-to-t from-background via-background/98 to-transparent pb-6 pt-2"
        >
          <div className="mx-auto max-w-2xl px-4 sm:px-6">
            <div className="rounded-2xl border border-border/60 bg-card p-1 shadow-xl shadow-black/[0.04] ring-1 ring-border/30 transition-shadow duration-200 focus-within:shadow-[0_20px_50px_-12px_rgba(139,92,246,0.15),0_0_0_1px_rgba(139,92,246,0.12)]">
              <Textarea
                id="q"
                placeholder="Ask one question to compare baseline vs MCP…"
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                rows={3}
                className="min-h-[88px] resize-none border-0 bg-transparent px-4 py-3 text-[15px] leading-relaxed shadow-none focus-visible:ring-0"
                onKeyDown={(e) => {
                  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                    e.preventDefault();
                    void runCompare();
                  }
                }}
              />
              <div className="flex items-center justify-between gap-3 border-t border-border/40 px-3 py-2">
                <span className="text-[11px] text-muted-foreground">
                  ⌘/Ctrl + Enter to run
                </span>
                <Button
                  size="sm"
                  className="gap-1.5 rounded-lg shadow-sm"
                  onClick={() => void runCompare()}
                  disabled={pending || !question.trim()}
                >
                  {pending ? (
                    <span className="inline-flex items-center gap-2">
                      <span className="size-3.5 animate-spin rounded-full border-2 border-primary-foreground/30 border-t-primary-foreground" />
                      Comparing…
                    </span>
                  ) : (
                    <>
                      <ArrowLeftRight className="size-3.5" />
                      Run comparison
                    </>
                  )}
                </Button>
              </div>
            </div>
          </div>
        </motion.div>
      </div>
    </div>
  );
}

function ComparisonColumn({
  label,
  hint,
  icon,
  lane,
  tone,
}: {
  label: string;
  hint: string;
  icon: React.ReactNode;
  lane: Lane;
  tone: "slate" | "violet";
}) {
  const bar =
    tone === "violet"
      ? "from-violet-500/90 to-indigo-500/80"
      : "from-slate-400/80 to-slate-500/70";

  return (
    <div
      className={cn(
        "relative flex flex-col",
        tone === "violet" ? "bg-violet-50/[0.35]" : "bg-transparent",
      )}
    >
      <div
        className={cn(
          "flex items-start justify-between gap-3 border-b border-border/40 px-5 py-3 sm:px-6",
          tone === "violet" && "bg-violet-50/50",
        )}
      >
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span
              className={cn(
                "inline-flex size-7 items-center justify-center rounded-lg",
                tone === "violet"
                  ? "bg-violet-100 text-violet-700"
                  : "bg-slate-100 text-slate-700",
              )}
            >
              {icon}
            </span>
            <div>
              <h2 className="text-sm font-semibold tracking-tight">{label}</h2>
              <p className="text-[11px] text-muted-foreground">{hint}</p>
            </div>
          </div>
        </div>
        <div className="flex shrink-0 flex-wrap justify-end gap-1">
          <Badge variant="outline" className="font-mono text-[10px] font-normal">
            {(lane.durationMs / 1000).toFixed(2)}s
          </Badge>
          {lane.usage?.totalTokens != null ? (
            <Badge variant="outline" className="font-mono text-[10px] font-normal">
              ~{lane.usage.totalTokens} tok
            </Badge>
          ) : null}
          {lane.toolSteps != null ? (
            <Badge variant="outline" className="font-mono text-[10px] font-normal">
              {lane.toolSteps} tools
            </Badge>
          ) : null}
        </div>
      </div>
      <div
        className={cn("h-0.5 w-full bg-gradient-to-r opacity-90", bar)}
        aria-hidden
      />

      <div className="flex-1 px-5 py-4 sm:px-6 sm:py-5">
        {lane.error ? (
          <Alert variant="destructive" className="text-xs">
            <AlertTitle className="text-xs">Failed</AlertTitle>
            <AlertDescription className="whitespace-pre-wrap font-mono text-[11px]">
              {lane.error}
            </AlertDescription>
          </Alert>
        ) : (
          <AssistantMarkdown
            text={lane.text || "—"}
            tone={tone === "violet" ? "violet" : "slate"}
          />
        )}
      </div>
    </div>
  );
}
