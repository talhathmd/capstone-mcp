import Markdown from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";
import type { Components } from "react-markdown";
import { cn } from "@/lib/utils";

type Tone = "slate" | "violet";

const heading =
  "mb-2 mt-3 text-sm font-semibold tracking-tight text-foreground first:mt-0";

export function AssistantMarkdown({
  text,
  tone,
}: {
  text: string;
  tone: Tone;
}) {
  const linkClass =
    tone === "violet"
      ? "font-medium text-violet-700 underline decoration-violet-400/55 underline-offset-[3px] transition-colors hover:text-violet-900"
      : "font-medium text-slate-700 underline decoration-slate-400/55 underline-offset-[3px] transition-colors hover:text-slate-900";

  const components: Components = {
    p: ({ children }) => (
      <p className="mb-3 text-[13px] leading-relaxed text-foreground/95 last:mb-0">
        {children}
      </p>
    ),
    strong: ({ children }) => (
      <strong className="font-semibold text-foreground">{children}</strong>
    ),
    em: ({ children }) => (
      <em className="italic text-foreground/95">{children}</em>
    ),
    a: ({ href, children }) => (
      <a
        href={href}
        target="_blank"
        rel="noopener noreferrer"
        className={linkClass}
      >
        {children}
      </a>
    ),
    ul: ({ children }) => (
      <ul className="my-2 list-disc space-y-1.5 pl-5 text-[13px] leading-relaxed marker:text-muted-foreground">
        {children}
      </ul>
    ),
    ol: ({ children }) => (
      <ol className="my-2 list-decimal space-y-1.5 pl-5 text-[13px] leading-relaxed marker:text-muted-foreground">
        {children}
      </ol>
    ),
    li: ({ children }) => <li className="pl-0.5">{children}</li>,
    h1: ({ children }) => <h3 className={heading}>{children}</h3>,
    h2: ({ children }) => <h3 className={heading}>{children}</h3>,
    h3: ({ children }) => <h3 className={heading}>{children}</h3>,
    h4: ({ children }) => <h4 className={cn(heading, "text-[13px]")}>{children}</h4>,
    blockquote: ({ children }) => (
      <blockquote className="my-2 border-l-2 border-muted-foreground/25 pl-3 text-[13px] text-muted-foreground italic">
        {children}
      </blockquote>
    ),
    hr: () => <hr className="my-4 border-border/60" />,
    table: ({ children }) => (
      <div className="my-2 overflow-x-auto rounded-lg border border-border/50">
        <table className="w-full border-collapse text-[13px]">{children}</table>
      </div>
    ),
    thead: ({ children }) => <thead className="bg-muted/40">{children}</thead>,
    tbody: ({ children }) => <tbody>{children}</tbody>,
    tr: ({ children }) => <tr className="border-b border-border/40">{children}</tr>,
    th: ({ children }) => (
      <th className="border border-border/50 px-2 py-1.5 text-left font-semibold">
        {children}
      </th>
    ),
    td: ({ children }) => (
      <td className="border border-border/50 px-2 py-1.5 align-top">{children}</td>
    ),
    code: ({ className, children, ...props }) => {
      const raw = String(children);
      const hasLang = Boolean(className?.includes("language-"));
      const isInline = !hasLang && !raw.includes("\n");
      if (isInline) {
        return (
          <code
            className="rounded-md bg-muted/90 px-1.5 py-0.5 font-mono text-[12px] text-foreground"
            {...props}
          >
            {children}
          </code>
        );
      }
      return (
        <code className={cn("font-mono text-[12px]", className)} {...props}>
          {children}
        </code>
      );
    },
    pre: ({ children }) => (
      <pre className="my-2 overflow-x-auto rounded-lg border border-border/50 bg-muted/50 p-3 font-mono text-[12px] leading-relaxed">
        {children}
      </pre>
    ),
  };

  return (
    <div className="assistant-markdown text-[13px] text-foreground/95">
      <Markdown remarkPlugins={[remarkGfm, remarkBreaks]} components={components}>
        {text.trim() ? text : "—"}
      </Markdown>
    </div>
  );
}
