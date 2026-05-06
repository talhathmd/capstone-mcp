"""
compare_runner.py - Run N benchmark questions with both OpenAI and Anthropic
and produce a side-by-side comparison CSV.

Usage:
    python -m benchmarks.compare_runner \
        --gold_csv gold_full_all.csv \
        --out_csv compare_out.csv \
        --n 20 \
        --mode mcp \
        --prompt_style smart \
        --openai_model gpt-4o \
        --anthropic_model claude-3-5-sonnet-20241022
"""
import argparse
import asyncio
import csv
import datetime as dt
import json
import os
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


def _load_dotenv(dotenv_path: Path) -> None:
    try:
        if not dotenv_path.exists():
            return
        for raw in dotenv_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip()
            if not key:
                continue
            if (val.startswith('"') and val.endswith('"')) or (
                val.startswith("'") and val.endswith("'")
            ):
                val = val[1:-1]
            if key not in os.environ:
                os.environ[key] = val
    except Exception:
        return


def _ensure_requirements() -> None:
    try:
        import httpx  # noqa: F401
        return
    except ModuleNotFoundError:
        pass

    in_venv = getattr(sys, "base_prefix", sys.prefix) != sys.prefix
    repo_root = Path(__file__).resolve().parents[1]
    req_file = repo_root / "requirements.txt"

    if in_venv:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", str(req_file)])
        return

    venv_dir = repo_root / ".venv"
    venv_python = venv_dir / "bin" / "python"
    venv_pip = venv_dir / "bin" / "pip"

    if not venv_python.exists():
        subprocess.check_call([sys.executable, "-m", "venv", str(venv_dir)])

    subprocess.check_call([str(venv_pip), "install", "-r", str(req_file)])
    os_args = [str(venv_python), "-m", "benchmarks.compare_runner", *sys.argv[1:]]
    os.execv(str(venv_python), os_args)


async def run_comparison(
    gold_csv: str,
    out_csv: str,
    n: int,
    timeout_ms: int,
    limit_cap: int,
    sleep_s: float,
    mode_str: str,
    prompt_style_str: str,
    llm_timeout_s: float,
    openai_model: str,
    anthropic_model: str,
) -> None:
    from benchmarks.qawiki_text2sparql_runner import (
        LLMConfig,
        Mode,
        PromptStyle,
        _parse_bool,
        _split_semicolon_ids,
        qids_to_semicolon_list,
        run_one,
    )

    mode = Mode(mode_str)
    prompt_style = PromptStyle(prompt_style_str)

    # Build provider configs
    configs: List[LLMConfig] = []

    openai_key = (os.getenv("OPENAI_API_KEY", "") or "").strip()
    if openai_key and openai_model:
        configs.append(
            LLMConfig(
                provider="openai",
                model=openai_model,
                api_key=openai_key,
                base_url=(os.getenv("OPENAI_BASE_URL", "") or "https://api.openai.com/v1/chat/completions").strip(),
            )
        )
    else:
        print("WARNING: OPENAI_API_KEY not set or --openai_model empty — skipping OpenAI.")

    anthropic_key = (
        os.getenv("ANTHROPIC_API_KEY", "")
        or os.getenv("ANTHORPIC_API_KEY", "")
        or ""
    ).strip()
    if anthropic_key and anthropic_model:
        configs.append(
            LLMConfig(
                provider="anthropic",
                model=anthropic_model,
                api_key=anthropic_key,
                base_url=(os.getenv("ANTHROPIC_BASE_URL", "") or "https://api.anthropic.com/v1/messages").strip(),
            )
        )
    else:
        print("WARNING: ANTHROPIC_API_KEY not set or --anthropic_model empty — skipping Anthropic.")

    if not configs:
        raise ValueError(
            "No LLM providers configured. Set OPENAI_API_KEY and/or ANTHROPIC_API_KEY in .env."
        )

    # Load gold CSV
    scorable: List[Dict[str, Any]] = []
    total_rows = 0
    skipped = 0
    with open(gold_csv, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        required = {"questionId", "question_en", "gold_ok", "gold_row_count", "gold_answer_ids"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Gold CSV missing required columns: {sorted(missing)}")
        has_values_col = "gold_answer_values" in (reader.fieldnames or [])

        for r in reader:
            total_rows += 1
            if not _parse_bool(r.get("gold_ok")):
                continue
            gold_row_count = int(r.get("gold_row_count") or 0)
            gold_ids = _split_semicolon_ids(r.get("gold_answer_ids", ""))
            gold_values = _split_semicolon_ids(r.get("gold_answer_values", "") if has_values_col else "")
            if gold_row_count > 0 and len(gold_ids) == 0 and len(gold_values) == 0:
                skipped += 1
                continue
            scorable.append(
                {
                    "questionId": r.get("questionId", ""),
                    "question_en": r.get("question_en", ""),
                    "gold_row_count": gold_row_count,
                    "gold_answer_ids": qids_to_semicolon_list(gold_ids),
                    "gold_answer_values": ";".join(gold_values),
                }
            )

    selected = scorable[: max(0, n)]
    n_run = len(selected)
    print(f"Gold rows: {total_rows} | Scorable: {len(scorable)} | Running: {n_run}")
    if skipped:
        print(f"Skipped (non-QID answers in old CSV): {skipped}")

    # Logs dir
    logs_dir = Path("benchmarks") / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")

    # Results store: provider_key -> list of rows (indexed by question)
    results_by_provider: Dict[str, List[Optional[Dict[str, Any]]]] = {}

    for cfg in configs:
        provider_key = f"{cfg.provider}/{cfg.model}"
        print(f"\n{'='*60}")
        print(f"Running: {provider_key}")
        print(f"{'='*60}")

        sem = asyncio.Semaphore(1)
        log_path = logs_dir / f"compare_{cfg.provider}_{ts}.jsonl"
        rows: List[Optional[Dict[str, Any]]] = []

        with open(log_path, "w", encoding="utf-8") as log_fp:
            for i, item in enumerate(selected):
                try:
                    row = await asyncio.wait_for(
                        run_one(
                            item=item,
                            cfg=cfg,
                            timeout_ms=timeout_ms,
                            limit_cap=limit_cap,
                            sleep_s=sleep_s,
                            sem=sem,
                            log_fp=log_fp,
                            mode=mode,
                            prompt_style=prompt_style,
                            llm_timeout_s=llm_timeout_s,
                        ),
                        timeout=llm_timeout_s * 6 + timeout_ms / 1000 + 10,
                    )
                except asyncio.TimeoutError:
                    print(f"  [{i+1}/{n_run}] TIMEOUT qid={item['questionId']}")
                    row = {
                        "questionId": item["questionId"],
                        "question_en": item["question_en"],
                        "exact_match": 0,
                        "f1": 0.0,
                        "pred_ok": False,
                        "pred_error_code": "RUNNER_TIMEOUT",
                        "pred_row_count": 0,
                        "pred_answer_ids": "",
                        "pred_answer_values": "",
                    }
                rows.append(row)
                f1_val = float(row.get("f1") or 0.0)
                print(
                    f"  [{i+1}/{n_run}] {item['question_en'][:55]:<55} "
                    f"f1={f1_val:.2f} ok={row.get('pred_ok')} err={row.get('pred_error_code','')[:20]}"
                )

        results_by_provider[provider_key] = rows

        f1s = [float(r.get("f1") or 0.0) for r in rows if r]
        exact = [int(r.get("exact_match") or 0) for r in rows if r]
        print(f"\n  {provider_key} summary: mean_f1={statistics.mean(f1s):.3f}  exact={sum(exact)}/{n_run}")

    # Build comparison CSV
    provider_keys = list(results_by_provider.keys())
    fieldnames = ["questionId", "question_en", "gold_answer_ids", "gold_answer_values"]
    for pk in provider_keys:
        safe = pk.replace("/", "_").replace("-", "_").replace(".", "_")
        fieldnames += [
            f"{safe}_f1",
            f"{safe}_exact",
            f"{safe}_pred_ok",
            f"{safe}_pred_ids",
            f"{safe}_error_code",
            f"{safe}_sparql",
        ]

    out_rows: List[Dict[str, Any]] = []
    for i, item in enumerate(selected):
        r: Dict[str, Any] = {
            "questionId": item["questionId"],
            "question_en": item["question_en"],
            "gold_answer_ids": item["gold_answer_ids"],
            "gold_answer_values": item.get("gold_answer_values", ""),
        }
        for pk in provider_keys:
            safe = pk.replace("/", "_").replace("-", "_").replace(".", "_")
            prow = (results_by_provider[pk][i] or {}) if i < len(results_by_provider[pk]) else {}
            r[f"{safe}_f1"] = f"{float(prow.get('f1') or 0.0):.4f}"
            r[f"{safe}_exact"] = prow.get("exact_match", 0)
            r[f"{safe}_pred_ok"] = prow.get("pred_ok", False)
            r[f"{safe}_pred_ids"] = prow.get("pred_answer_ids", "")
            r[f"{safe}_error_code"] = prow.get("pred_error_code", "")
            r[f"{safe}_sparql"] = (prow.get("pred_sparql") or "")[:300]
        out_rows.append(r)

    with open(out_csv, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(out_rows)

    print(f"\nComparison CSV written: {out_csv}")

    # Final side-by-side summary
    print("\n" + "=" * 80)
    print(f"{'SIDE-BY-SIDE SUMMARY':^80}")
    print("=" * 80)
    header = f"{'Question':<45}" + "".join(f"  {pk.split('/')[1][:12]:>12}" for pk in provider_keys)
    print(header)
    print("-" * len(header))
    for i, item in enumerate(selected):
        q = item["question_en"][:44]
        row_str = f"{q:<45}"
        for pk in provider_keys:
            prow = results_by_provider[pk][i] or {}
            f1_val = float(prow.get("f1") or 0.0)
            row_str += f"  {f1_val:>12.2f}"
        print(row_str)
    print("-" * len(header))
    totals = f"{'Mean F1':<45}"
    for pk in provider_keys:
        f1s = [float((results_by_provider[pk][i] or {}).get("f1") or 0.0) for i in range(n_run)]
        totals += f"  {statistics.mean(f1s):>12.3f}"
    print(totals)
    exact_line = f"{'Exact Match Rate':<45}"
    for pk in provider_keys:
        exacts = [int((results_by_provider[pk][i] or {}).get("exact_match") or 0) for i in range(n_run)]
        exact_line += f"  {sum(exacts)/n_run:>12.3f}"
    print(exact_line)
    print("=" * 80)


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    _load_dotenv(repo_root / ".env")
    _ensure_requirements()

    parser = argparse.ArgumentParser(
        description="Compare OpenAI vs Anthropic on Wikidata text-to-SPARQL benchmark"
    )
    parser.add_argument("--gold_csv", required=True, help="Gold CSV (use gold_full_all.csv)")
    parser.add_argument("--out_csv", required=True, help="Output comparison CSV")
    parser.add_argument("--n", type=int, default=20, help="Number of questions to run")
    parser.add_argument("--timeout_ms", type=int, default=30000, help="SPARQL query timeout (ms)")
    parser.add_argument("--limit_cap", type=int, default=200)
    parser.add_argument("--sleep_s", type=float, default=0.5, help="Sleep between questions")
    parser.add_argument("--llm_timeout_s", type=float, default=60.0, help="Per-LLM-call timeout (s)")
    parser.add_argument(
        "--mode",
        choices=["direct", "mcp"],
        default="mcp",
        help="Prediction mode: direct or mcp",
    )
    parser.add_argument(
        "--prompt_style",
        choices=["dumb", "smart"],
        default="smart",
        help="Prompt style: dumb or smart",
    )
    parser.add_argument(
        "--openai_model",
        default="gpt-4o",
        help="OpenAI model name (default: gpt-4o)",
    )
    parser.add_argument(
        "--anthropic_model",
        default="claude-3-5-sonnet-20241022",
        help="Anthropic model name (default: claude-3-5-sonnet-20241022)",
    )
    args = parser.parse_args()

    asyncio.run(
        run_comparison(
            gold_csv=args.gold_csv,
            out_csv=args.out_csv,
            n=args.n,
            timeout_ms=args.timeout_ms,
            limit_cap=args.limit_cap,
            sleep_s=args.sleep_s,
            mode_str=args.mode,
            prompt_style_str=args.prompt_style,
            llm_timeout_s=args.llm_timeout_s,
            openai_model=args.openai_model,
            anthropic_model=args.anthropic_model,
        )
    )


if __name__ == "__main__":
    main()
