import csv
from collections import Counter
from pathlib import Path
from statistics import mean, median


ROOT = Path(__file__).resolve().parents[1]
SCORED_CSV = ROOT / "benchmark_100_scored.csv"
STATUS_COUNTER_CSV = ROOT / "benchmark_status_counter.csv"
SUMMARY_CSV = ROOT / "benchmark_summary.csv"
COMPARE_CSV = ROOT / "compare_mcp_vs_direct_dumb_summary.csv"


def _as_float(value: str, default: float = 0.0) -> float:
    try:
        if value is None or str(value).strip() == "":
            return default
        return float(value)
    except Exception:
        return default


def _is_true(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "t", "yes", "y"}


def _is_ask_query(sparql: str) -> bool:
    return str(sparql or "").strip().upper().startswith("ASK")


def _is_nonempty(value: str) -> bool:
    return str(value or "").strip() != ""


def main() -> None:
    with SCORED_CSV.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))

    status_counter: Counter[str] = Counter((r.get("status") or "").strip() for r in rows)

    n_total = len(rows)
    n_gold_ok = sum(1 for r in rows if _is_true(r.get("gold_ok", "")))
    ask_rows = [r for r in rows if _is_ask_query(r.get("gold_sparql", ""))]
    n_yesno_ask = len(ask_rows)

    unscorable_rows = [r for r in rows if (r.get("status") or "").startswith("UNSCORABLE_")]
    n_unscorable_total = len(unscorable_rows)
    n_unscorable_ask = sum(1 for r in unscorable_rows if (r.get("status") or "") == "UNSCORABLE_ASK")
    n_unscorable_non_ask = n_unscorable_total - n_unscorable_ask

    entity_scorable_rows = [r for r in rows if (r.get("status") or "") in {"PASS", "PARTIAL_MATCH", "WRONG_ANSWER", "PRED_EMPTY"}]
    n_scorable_entity_qid = len(entity_scorable_rows)

    literal_candidate_rows = [
        r
        for r in rows
        if _is_true(r.get("gold_ok", ""))
        and int(_as_float(r.get("gold_row_count", "0"))) > 0
        and not _is_ask_query(r.get("gold_sparql", ""))
        and not _is_nonempty(r.get("gold_answer_ids", ""))
    ]
    n_scorable_literal_non_qid = len(literal_candidate_rows)

    exact_vals = [_as_float(r.get("exact_match", "0")) for r in entity_scorable_rows]
    f1_vals = [_as_float(r.get("f1", "0")) for r in entity_scorable_rows]
    latency_vals = [_as_float(r.get("pred_elapsed_ms", ""), default=-1.0) for r in entity_scorable_rows]
    latency_vals = [v for v in latency_vals if v >= 0]

    ask_has_pred = sum(1 for r in ask_rows if str(r.get("pred_sparql", "")).strip() != "")
    ask_pred_ok = sum(1 for r in ask_rows if _is_true(r.get("pred_ok", "")))
    literal_pred_nonempty = sum(
        1
        for r in literal_candidate_rows
        if _is_true(r.get("pred_ok", "")) and int(_as_float(r.get("pred_row_count", "0"))) > 0
    )

    pred_exec_success_all = sum(1 for r in rows if _is_true(r.get("pred_ok", "")))
    pred_exec_success_rate_all = (pred_exec_success_all / n_total) if n_total else 0.0

    exact_match_rate_scorable = mean(exact_vals) if exact_vals else 0.0
    mean_f1_scorable = mean(f1_vals) if f1_vals else 0.0
    median_f1_scorable = median(f1_vals) if f1_vals else 0.0
    latency_median = median(latency_vals) if latency_vals else 0.0
    latency_p90 = 0.0
    if latency_vals:
        xs = sorted(latency_vals)
        idx = max(0, min(len(xs) - 1, int((0.9 * len(xs) + 0.999999)) - 1))
        latency_p90 = xs[idx]

    eval_coverage_rate = ((n_scorable_entity_qid + n_yesno_ask) / n_gold_ok) if n_gold_ok else 0.0
    ask_coverage_rate = (ask_has_pred / n_yesno_ask) if n_yesno_ask else 0.0
    ask_exec_success_rate = (ask_pred_ok / n_yesno_ask) if n_yesno_ask else 0.0
    literal_nonempty_rate = (
        literal_pred_nonempty / n_scorable_literal_non_qid if n_scorable_literal_non_qid else 0.0
    )
    unscorable_rate_gold_ok = (n_unscorable_total / n_gold_ok) if n_gold_ok else 0.0

    with STATUS_COUNTER_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["status", "count"])
        for status, count in status_counter.most_common():
            if status:
                w.writerow([status, count])

    summary_rows = [
        ("N_total", float(n_total)),
        ("N_gold_ok", float(n_gold_ok)),
        ("N_scorable_entity_QID", float(n_scorable_entity_qid)),
        ("N_yesno_ask", float(n_yesno_ask)),
        ("N_scorable_literal_non_qid", float(n_scorable_literal_non_qid)),
        ("N_unscorable_total", float(n_unscorable_total)),
        ("N_unscorable_non_ask", float(n_unscorable_non_ask)),
        ("Eval_coverage_rate_gold_ok", round(eval_coverage_rate, 3)),
        ("Pred_exec_success_rate_all", round(pred_exec_success_rate_all, 3)),
        ("Exact_match_rate_scorable", round(exact_match_rate_scorable, 3)),
        ("Mean_F1_scorable", round(mean_f1_scorable, 3)),
        ("Median_F1_scorable", round(median_f1_scorable, 3)),
        ("Pred_latency_median_ms_scorable", round(latency_median, 1)),
        ("Pred_latency_p90_ms_scorable", round(latency_p90, 1)),
        ("ASK_pred_query_coverage", round(ask_coverage_rate, 3)),
        ("ASK_exec_success_rate", round(ask_exec_success_rate, 3)),
        ("Literal_pred_nonempty_rate", round(literal_nonempty_rate, 3)),
        ("Unscorable_rate_over_gold_ok", round(unscorable_rate_gold_ok, 3)),
    ]
    with SUMMARY_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["metric", "value"])
        w.writerows(summary_rows)

    compare_rows = [
        ["metric", "value"],
        ["condition", "mcp+smart (manual preds)"],
        ["N_scorable_entity_QID", n_scorable_entity_qid],
        ["N_yesno_ask", n_yesno_ask],
        ["N_scorable_literal_non_qid", n_scorable_literal_non_qid],
        ["N_unscorable_total", n_unscorable_total],
        ["Eval_coverage_rate_gold_ok", round(eval_coverage_rate, 3)],
        ["Exact_match_rate", round(exact_match_rate_scorable, 3)],
        ["Mean_F1", round(mean_f1_scorable, 3)],
        ["Median_F1", round(median_f1_scorable, 3)],
        ["Latency_median_ms", round(latency_median, 1)],
        ["Latency_p90_ms", round(latency_p90, 1)],
        ["ASK_pred_query_coverage", round(ask_coverage_rate, 3)],
        ["ASK_exec_success_rate", round(ask_exec_success_rate, 3)],
        ["Literal_pred_nonempty_rate", round(literal_nonempty_rate, 3)],
        ["condition", "direct+dumb (no grounding)"],
        ["N_scorable_entity_QID", 47],
        ["Exact_match_rate", 0.043],
        ["Mean_F1", 0.043],
        ["Median_F1", 0.0],
        ["Empty_pred_rate", 0.872],
        ["Latency_median_ms", 1089.0],
        ["Latency_p90_ms", 1253.1],
        ["condition", "Delta (MCP - Direct), entity-scorable only"],
        ["Exact_match_rate", round(exact_match_rate_scorable - 0.043, 3)],
        ["Mean_F1", round(mean_f1_scorable - 0.043, 3)],
    ]
    with COMPARE_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerows(compare_rows)


if __name__ == "__main__":
    main()
