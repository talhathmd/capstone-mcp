import argparse
import asyncio
import csv
import math
import re
import statistics
import os
import subprocess
import sys
from collections import Counter
from typing import Any, Dict, List, Optional


_QID_FROM_WD_ENTITY_RE = re.compile(
    r"/entity/(Q\d+)\b", re.IGNORECASE
)

def _load_dotenv(dotenv_path) -> None:
    """
    Minimal .env loader (no external deps).
    Does not override existing environment variables.
    """
    try:
        from pathlib import Path

        p = Path(dotenv_path)
        if not p.exists():
            return
        for raw in p.read_text(encoding="utf-8").splitlines():
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
    """
    Ensure runtime deps for the Wikidata runner are installed.

    This script is meant to be runnable via `python -m ...` even on fresh
    machines. If `httpx` is missing, we create a local `.venv` and install
    `requirements.txt`, then re-exec into that interpreter.
    """
    try:
        import httpx  # noqa: F401
        return
    except ModuleNotFoundError:
        pass

    in_venv = getattr(sys, "base_prefix", sys.prefix) != sys.prefix
    from pathlib import Path
    repo_root = Path(__file__).resolve().parents[1]
    req_file = repo_root / "requirements.txt"

    # If we're already in a venv, just install into it.
    if in_venv:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", str(req_file)])
        return

    venv_dir = repo_root / ".venv"
    venv_python = venv_dir / "bin" / "python"
    venv_pip = venv_dir / "bin" / "pip"
    req_file = repo_root / "requirements.txt"

    if not venv_python.exists():
        subprocess.check_call([sys.executable, "-m", "venv", str(venv_dir)])

    subprocess.check_call([str(venv_pip), "install", "-r", str(req_file)])

    os_args = [str(venv_python), "-m", "benchmarks.qawiki_gold_runner", *sys.argv[1:]]
    os.execv(str(venv_python), os_args)


def parse_question_en(questions_cell: str) -> Optional[str]:
    """
    Extract the English segment from a QAWiki `questions` cell.

    Expected shape (examples):
      "...@en|...@es|..."

    Rule:
      - split on '|'
      - pick the segment that ends with '@en'
      - strip the '@en' suffix
    """
    if not questions_cell:
        return None

    parts = [p.strip() for p in questions_cell.split("|")]
    for part in parts:
        if part.endswith("@en"):
            return part[: -len("@en")].strip()
    return None


def extract_answer_qids(rows: List[Dict[str, str]], limit: int = 50) -> List[str]:
    """
    Extract QIDs from any binding value that looks like:
      http://www.wikidata.org/entity/Q123

    Returns:
      - deduplicated
      - numerically sorted
      - truncated to `limit`
    """
    qids = set()
    for row in rows:
        for val in row.values():
            if not val:
                continue
            for m in _QID_FROM_WD_ENTITY_RE.finditer(str(val)):
                qids.add(m.group(1))

    qids_sorted = sorted(qids, key=lambda q: int(q[1:]))
    return qids_sorted[:limit]


def extract_answer_values(rows: List[Dict[str, str]], limit: int = 50) -> List[str]:
    """
    Extract non-QID answer values (dates, years, numbers, strings, booleans).
    """
    values: List[str] = []
    seen = set()
    for row in rows:
        for val in row.values():
            sval = str(val or "").strip()
            if not sval:
                continue
            # Keep literal/boolean answers here; QIDs are already in gold_answer_ids.
            if _QID_FROM_WD_ENTITY_RE.search(sval):
                continue
            if sval not in seen:
                seen.add(sval)
                values.append(sval)
            if len(values) >= limit:
                return values
    return values


def qids_to_semicolon_list(qids: List[str]) -> str:
    return ";".join(qids)


def p90(values: List[int]) -> Optional[int]:
    if not values:
        return None
    xs = sorted(values)
    # nearest-rank method
    idx = int(math.ceil(0.9 * len(xs))) - 1
    idx = max(0, min(idx, len(xs) - 1))
    return xs[idx]


async def run_benchmark(
    tsv_path: str,
    out_path: str,
    n: int,
    timeout_ms: int,
    limit_cap: int,
) -> None:
    # Import after bootstrap so missing deps don't crash the initial process.
    from tools.wikidata import run_sparql_wikidata_core

    total_rows = 0
    filtered_rows = 0
    candidates: List[Dict[str, Any]] = []

    # Read TSV
    with open(tsv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        expected_cols = {"questionId", "qId", "questions", "paraphrases", "sparql"}
        missing = expected_cols - set(reader.fieldnames or [])
        if missing:
            raise ValueError(
                f"TSV missing expected columns: {sorted(missing)}. "
                f"Found: {reader.fieldnames}"
            )

        for r in reader:
            total_rows += 1
            q_en = parse_question_en(r.get("questions", ""))
            if q_en is None:
                continue

            filtered_rows += 1
            candidates.append(
                {
                    "questionId": r.get("questionId", ""),
                    "question_en": q_en,
                    "gold_sparql": r.get("sparql", ""),
                }
            )

    selected = candidates[: max(0, n)]
    attempts = len(selected)

    rows_out: List[Dict[str, Any]] = []
    success_count = 0
    elapsed_samples: List[int] = []
    error_counter: Counter[str] = Counter()

    for i, item in enumerate(selected):
        qid = item["questionId"]
        gold_sparql = item["gold_sparql"]

        result = await run_sparql_wikidata_core(
            gold_sparql,
            timeout_ms=timeout_ms,
            limit_cap=limit_cap,
            allowed_entities=None,
            allowed_properties=None,
            allow_unbounded_property_paths=True,
        )

        gold_ok = bool(result.get("ok", False))
        stat = result.get("stats", {}) if isinstance(result.get("stats", {}), dict) else {}
        gold_elapsed_ms = stat.get("elapsed_ms")
        if isinstance(gold_elapsed_ms, str) and gold_elapsed_ms.isdigit():
            gold_elapsed_ms = int(gold_elapsed_ms)

        if gold_ok:
            success_count += 1
            gold_rows = result.get("rows", [])
            gold_row_count = int(result.get("row_count", 0) or 0)
            gold_answer_ids = extract_answer_qids(gold_rows)
            gold_answer_values = extract_answer_values(gold_rows)
        else:
            gold_row_count = int(result.get("row_count", 0) or 0)
            gold_answer_ids = []
            gold_answer_values = []

        gold_error_code = result.get("error_code", "") if not gold_ok else ""
        gold_error_message = (
            result.get("error_message", "") if not gold_ok else ""
        )

        if isinstance(gold_elapsed_ms, int):
            elapsed_samples.append(gold_elapsed_ms)

        if not gold_ok:
            error_counter[gold_error_code or "UNKNOWN"] += 1

        print(
            f"[{i + 1}/{attempts}] questionId={qid} ok={gold_ok} "
            f"row_count={gold_row_count} elapsed_ms={gold_elapsed_ms} "
            f"error_code={gold_error_code or '-'}"
        )

        rows_out.append(
            {
                "questionId": item["questionId"],
                "question_en": item["question_en"],
                "gold_sparql": gold_sparql,
                "gold_ok": gold_ok,
                "gold_row_count": gold_row_count,
                "gold_elapsed_ms": gold_elapsed_ms if gold_elapsed_ms is not None else "",
                "gold_answer_ids": qids_to_semicolon_list(gold_answer_ids),
                "gold_answer_values": ";".join(gold_answer_values),
                "gold_error_code": gold_error_code,
                "gold_error_message": gold_error_message,
            }
        )

    fieldnames = [
        "questionId",
        "question_en",
        "gold_sparql",
        "gold_ok",
        "gold_row_count",
        "gold_elapsed_ms",
        "gold_answer_ids",
        "gold_answer_values",
        "gold_error_code",
        "gold_error_message",
    ]

    # Write CSV
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows_out)

    # Summary
    gold_success_rate = (success_count / attempts) if attempts else 0.0
    median_latency_ms = statistics.median(elapsed_samples) if elapsed_samples else None
    p90_latency_ms = p90(elapsed_samples)
    most_common_error_codes = error_counter.most_common(10)

    print("\nGold benchmark summary")
    print(f"total_rows: {total_rows}")
    print(f"filtered_rows: {filtered_rows}")
    print(f"attempted_rows: {attempts}")
    print(f"gold_success_rate: {gold_success_rate:.3f}")
    print(f"median_latency_ms: {median_latency_ms}")
    print(f"p90_latency_ms: {p90_latency_ms}")
    print(f"most_common_error_codes: {most_common_error_codes}")


def main() -> None:
    from pathlib import Path
    repo_root = Path(__file__).resolve().parents[1]
    _load_dotenv(repo_root / ".env")
    _ensure_requirements()

    parser = argparse.ArgumentParser()
    parser.add_argument("--tsv", required=True, help="Path to QAWiki TSV")
    parser.add_argument("--out", required=True, help="Output CSV path")
    parser.add_argument("--n", type=int, default=10, help="Number of examples to run")
    parser.add_argument(
        "--timeout_ms",
        type=int,
        default=30000,
        help="Max execution time per query (ms)",
    )
    parser.add_argument(
        "--limit_cap",
        type=int,
        default=200,
        help="Max LIMIT allowed by safety linter",
    )

    args = parser.parse_args()
    asyncio.run(
        run_benchmark(
            tsv_path=args.tsv,
            out_path=args.out,
            n=args.n,
            timeout_ms=args.timeout_ms,
            limit_cap=args.limit_cap,
        )
    )


if __name__ == "__main__":
    main()

