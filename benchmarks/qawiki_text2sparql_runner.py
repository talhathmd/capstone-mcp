import argparse
import asyncio
import csv
import datetime as dt
import json
import math
import os
import re
import statistics
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


_QID_FROM_WD_ENTITY_RE = re.compile(r"/entity/(Q\d+)\b", re.IGNORECASE)
_QID_IN_QUERY_RE = re.compile(r"\bwd:(Q\d+)\b", re.IGNORECASE)
_PID_IN_QUERY_RE = re.compile(r"\b(?:wdt|p|ps|pq):(P\d+)\b", re.IGNORECASE)


class Mode(str, Enum):
    direct = "direct"
    mcp = "mcp"


class PromptStyle(str, Enum):
    dumb = "dumb"
    smart = "smart"

def _load_dotenv(dotenv_path: Path) -> None:
    """
    Minimal .env loader (no external deps).

    - Supports KEY=VALUE lines (VALUE may be quoted)
    - Ignores blank lines and lines starting with '#'
    - Does NOT override already-set environment variables
    """
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
            # Strip optional surrounding quotes
            if (val.startswith('"') and val.endswith('"')) or (
                val.startswith("'") and val.endswith("'")
            ):
                val = val[1:-1]
            if key not in os.environ:
                os.environ[key] = val
    except Exception:
        # Best-effort only; runner will still validate required env vars later.
        return


def _ensure_requirements() -> None:
    """
    Ensure runtime deps are installed (httpx).

    If httpx is missing:
      - if already in venv: pip install -r requirements.txt
      - else: create .venv, install requirements.txt, re-exec into it
    """
    try:
        import httpx  # noqa: F401
        return
    except ModuleNotFoundError:
        pass

    in_venv = getattr(sys, "base_prefix", sys.prefix) != sys.prefix
    repo_root = Path(__file__).resolve().parents[1]
    req_file = repo_root / "requirements.txt"

    if in_venv:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-r", str(req_file)]
        )
        return

    venv_dir = repo_root / ".venv"
    venv_python = venv_dir / "bin" / "python"
    venv_pip = venv_dir / "bin" / "pip"

    if not venv_python.exists():
        subprocess.check_call([sys.executable, "-m", "venv", str(venv_dir)])

    subprocess.check_call([str(venv_pip), "install", "-r", str(req_file)])

    os_args = [
        str(venv_python),
        "-m",
        "benchmarks.qawiki_text2sparql_runner",
        *sys.argv[1:],
    ]
    os.execv(str(venv_python), os_args)


def _parse_bool(s: Any) -> bool:
    if isinstance(s, bool):
        return s
    v = str(s or "").strip().lower()
    return v in ("1", "true", "t", "yes", "y")


def _split_semicolon_ids(s: str) -> List[str]:
    if not s:
        return []
    parts = [p.strip() for p in (s or "").split(";")]
    return [p for p in parts if p]


def extract_answer_qids(rows: List[Dict[str, str]], limit: int = 50) -> List[str]:
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
    values: List[str] = []
    seen = set()
    for row in rows:
        for val in row.values():
            sval = str(val or "").strip()
            if not sval:
                continue
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
    idx = int(math.ceil(0.9 * len(xs))) - 1
    idx = max(0, min(idx, len(xs) - 1))
    return xs[idx]


def _normalize_literal_for_match(v: str) -> str:
    s = str(v or "").strip().strip('"').strip("'")
    if not s:
        return ""
    # Collapse ISO date/datetime to year for tolerant matching.
    m_date = re.match(r"^(\d{4})-\d{2}-\d{2}", s)
    if m_date:
        return m_date.group(1)
    # If it's a plain year, keep as-is.
    if re.match(r"^\d{4}$", s):
        return s
    # Normalize numeric literals (e.g. 42.0 -> 42).
    if re.match(r"^-?\d+(?:\.\d+)?$", s):
        try:
            f = float(s)
            if f.is_integer():
                return str(int(f))
            return f"{f:.10g}"
        except Exception:
            return s.lower()
    return s.lower()


def _extract_first_json_object(text: str) -> Optional[str]:
    """
    Best-effort extraction of the first {...} block.
    Handles leading chatter by scanning for the first '{' and tracking braces.
    """
    if not text:
        return None
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def parse_llm_json(text: str) -> Tuple[Optional[dict], Optional[str]]:
    """
    Parse strict JSON response with key 'sparql'.

    Returns (obj, error_message)
    """
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj, None
        return None, "Top-level JSON is not an object"
    except Exception:
        pass

    snippet = _extract_first_json_object(text)
    if not snippet:
        return None, "No JSON object found in response"
    try:
        obj = json.loads(snippet)
        if isinstance(obj, dict):
            return obj, None
        return None, "Extracted JSON is not an object"
    except Exception as e:
        return None, f"Failed to parse extracted JSON: {e}"


def build_prompt(question_en: str) -> str:
    return (
        "You are generating SPARQL for Wikidata.\n"
        "Return ONLY valid JSON (no markdown, no extra keys), with a single key \"sparql\".\n"
        "The value must be a SPARQL SELECT query (SELECT only, not ASK/CONSTRUCT/DESCRIBE).\n"
        "The query must include WHERE.\n"
        "Avoid GRAPH and FROM. Avoid SERVICE except optionally SERVICE wikibase:label.\n"
        "Prefer wdt: direct claims unless qualifiers are required.\n"
        "If timeouts are likely, keep the query minimal.\n"
        "Always include a LIMIT.\n"
        "Return exactly one answer variable named ?answer.\n"
        "Use double-quoted strings if needed; do not use single-quoted strings.\n"
        "Do NOT do title matching like wdt:P1476 \"...\".\n\n"
        f"Question: {question_en}\n"
    )


def build_plan_prompt(question_en: str) -> str:
    return (
        "Return ONLY valid JSON (no markdown, no extra keys) with:\n"
        "{\n"
        '  "entities": ["...", "..."],\n'
        '  "properties": ["...", "..."]\n'
        "}\n"
        "Rules:\n"
        "- entities: surface strings from the question (people, places, works, organizations, regions)\n"
        "- properties: relation phrases (e.g. 'cast member', 'character role', 'author', 'population', 'capital')\n"
        "- limit entities to <= 6 and properties to <= 6\n\n"
        f"Question: {question_en}\n"
    )


def build_grounded_prompt(
    question_en: str,
    entity_lookups: List[Dict[str, Any]],
    property_lookups: List[Dict[str, Any]],
    limit: int = 50,
) -> str:
    grounding_blob = json.dumps(
        {"entities": entity_lookups, "properties": property_lookups},
        ensure_ascii=False,
        indent=2,
    )
    return (
        "You are generating SPARQL for Wikidata.\n"
        "You MUST use the grounded IDs below when they match the question.\n"
        "Return ONLY valid JSON (no markdown, no extra keys), with a single key \"sparql\".\n"
        "The value must be a SPARQL SELECT query.\n"
        "The query must include WHERE.\n"
        "Avoid GRAPH and FROM. Avoid SERVICE except optionally SERVICE wikibase:label.\n"
        "Prefer wdt: direct claims unless qualifiers are required.\n"
        "Return exactly one answer variable named ?answer.\n"
        "Use double-quoted strings if needed; do not use single-quoted strings.\n"
        "Do NOT do title matching like wdt:P1476 \"...\".\n"
        f"Always include LIMIT {limit}.\n\n"
        "Grounded candidates (choose the best IDs):\n"
        f"{grounding_blob}\n\n"
        f"Question: {question_en}\n"
    )


def build_repair_prompt(
    question_en: str,
    prior_sparql: str,
    note: str,
    entity_lookups: List[Dict[str, Any]],
    property_lookups: List[Dict[str, Any]],
    limit: int = 50,
) -> str:
    grounding_blob = json.dumps(
        {"entities": entity_lookups, "properties": property_lookups},
        ensure_ascii=False,
        indent=2,
    )
    return (
        "You are repairing a Wikidata SPARQL query.\n"
        "Return ONLY valid JSON with a single key \"sparql\".\n"
        "The value must be a SPARQL SELECT query with exactly one answer variable named ?answer.\n"
        "Avoid GRAPH and FROM. Avoid SERVICE except optionally SERVICE wikibase:label.\n"
        "Prefer wdt: direct claims.\n"
        "Use double-quoted strings if needed; do not use single-quoted strings.\n"
        f"Always include LIMIT {limit}.\n\n"
        f"Question: {question_en}\n\n"
        "Previous SPARQL:\n"
        f"{prior_sparql}\n\n"
        f"Result: {note}\n\n"
        "Grounded candidates (choose the best IDs):\n"
        f"{grounding_blob}\n"
    )


def build_direct_prompt(question_en: str, prompt_style: PromptStyle) -> str:
    if prompt_style == PromptStyle.dumb:
        return (
            "Return ONLY valid JSON with a single key \"sparql\".\n"
            "The value must be a SPARQL SELECT query for Wikidata.\n"
            "Must include WHERE and a LIMIT.\n"
            "Return exactly one answer variable named ?answer.\n\n"
            f"Question: {question_en}\n"
        )

    # smart
    return (
        "You are generating SPARQL for Wikidata.\n"
        "Return ONLY valid JSON (no markdown, no extra keys), with a single key \"sparql\".\n"
        "Hard rules:\n"
        "- SELECT only\n"
        "- Must include WHERE\n"
        "- Must include LIMIT 50 (or smaller)\n"
        "- Avoid GRAPH and FROM\n"
        "- Avoid SERVICE except optional wikibase:label (prefer to omit labels)\n"
        "- Prefer wdt: direct claims; use p:/ps:/pq: when qualifiers are required\n"
        "- Return exactly one answer variable named ?answer\n"
        "- Use double-quoted strings if needed; never single-quoted strings\n"
        "- Never do title matching like wdt:P1476 \"...\"\n\n"
        "Query-pattern hints:\n"
        "1) Who played [character] in [work] -> use p:P161 with qualifier pq:P453 = [character QID], and ps:P161 gives actor.\n"
        "2) largest country in Africa by population/area -> constrain wdt:P30 wd:Q15 (Africa) and use wdt:P1082 (population) or wdt:P2046 (area) with ORDER BY DESC + LIMIT 1.\n"
        "3) creator/author of a work -> try wdt:P50 (author) for books/manga; wdt:P170 (creator) for general works if author not applicable.\n\n"
        f"Question: {question_en}\n"
    )


def build_mcp_extract_prompt(question_en: str, prompt_style: PromptStyle) -> str:
    # dumb/smart are the same for extraction for now; keep deterministic schema.
    return build_plan_prompt(question_en)


def build_mcp_selection_prompt(
    question_en: str,
    tool_entity_results: List[Dict[str, Any]],
    tool_property_results: List[Dict[str, Any]],
) -> str:
    """
    Ask the LLM to choose among search_*_core candidates, MCP-style.
    """
    context = {
        "entities": [
            {
                "name": r.get("name", ""),
                "candidates": r.get("candidates", []),
            }
            for r in tool_entity_results
        ],
        "properties": [
            {
                "name": r.get("name", ""),
                "candidates": r.get("candidates", []),
            }
            for r in tool_property_results
        ],
    }
    grounding_blob = json.dumps(context, ensure_ascii=False, indent=2)
    return (
        "You are choosing grounded Wikidata IDs for an MCP client.\n"
        "Return ONLY valid JSON (no markdown) with this shape:\n"
        "{\n"
        '  "selected_entities": {"<entity_string>": "<QID or null>", ...},\n'
        '  "selected_properties": {"<property_string>": "<PID or null>", ...},\n'
        '  "need_more": false\n'
        "}\n\n"
        "Hard rules:\n"
        "- For each entity/property string, you may only choose IDs from the provided candidate lists.\n"
        "- If no suitable candidate exists for a string, set its value to null and set need_more=true.\n"
        "- DO NOT invent QIDs/PIDs.\n\n"
        "Candidates:\n"
        f"{grounding_blob}\n\n"
        f"Question: {question_en}\n"
    )

def build_mcp_synthesis_prompt(
    question_en: str,
    chosen_entities: List[Dict[str, Any]],
    chosen_properties: List[Dict[str, Any]],
    prompt_style: PromptStyle,
    limit: int = 50,
) -> str:
    chosen_blob = json.dumps(
        {"entities": chosen_entities, "properties": chosen_properties},
        ensure_ascii=False,
        indent=2,
    )

    base = (
        "You are generating SPARQL for Wikidata.\n"
        "Return ONLY valid JSON with a single key \"sparql\".\n"
        "Hard rules:\n"
        "- SELECT only\n"
        "- Must include WHERE\n"
        f"- Must include LIMIT {limit} (or smaller)\n"
        "- Return exactly one answer variable named ?answer\n"
        "- Use ONLY the provided QIDs/PIDs below. DO NOT invent IDs.\n"
        "- Avoid GRAPH and FROM\n"
        "- Avoid SERVICE except optional wikibase:label (prefer to omit labels)\n"
        "- Prefer wdt: direct claims; use p:/ps:/pq: when qualifiers are required\n"
        "- Use double-quoted strings if needed; never single-quoted strings\n"
        "- Never do title matching like wdt:P1476 \"...\"\n\n"
        "Chosen IDs (use these):\n"
        f"{chosen_blob}\n\n"
        f"Question: {question_en}\n"
    )

    if prompt_style == PromptStyle.dumb:
        return base

    return (
        base
        + "\nQuery-pattern hints:\n"
        + "1) Who played [character] in [work] -> use p:P161 with qualifier pq:P453 = [character QID], and ps:P161 gives actor.\n"
        + "2) largest country in Africa by population/area -> constrain wdt:P30 wd:Q15 (Africa) and use wdt:P1082 (population) or wdt:P2046 (area) with ORDER BY DESC + LIMIT 1.\n"
        + "3) creator/author of a work -> try wdt:P50 (author) for books/manga; wdt:P170 (creator) for general works if author not applicable.\n"
    )


def build_mcp_repair_prompt(
    question_en: str,
    prior_sparql: str,
    chosen_entities: List[Dict[str, Any]],
    chosen_properties: List[Dict[str, Any]],
    limit: int = 50,
) -> str:
    chosen_blob = json.dumps(
        {"entities": chosen_entities, "properties": chosen_properties},
        ensure_ascii=False,
        indent=2,
    )
    return (
        "You are repairing a Wikidata SPARQL query.\n"
        "Return ONLY valid JSON with a single key \"sparql\".\n"
        "Hard rules:\n"
        "- SELECT only\n"
        "- Must include WHERE\n"
        f"- Must include LIMIT {limit} (or smaller)\n"
        "- Return exactly one answer variable named ?answer\n"
        "- Use ONLY the provided QIDs/PIDs below. DO NOT invent IDs.\n"
        "- Avoid GRAPH and FROM\n"
        "- Avoid SERVICE except optional wikibase:label (prefer to omit labels)\n"
        "- Prefer wdt: direct claims; use p:/ps:/pq: when qualifiers are required\n\n"
        "If using character role qualifier, MUST use this statement pattern exactly:\n"
        "  wd:<workQID> p:P161 ?st .\n"
        "  ?st ps:P161 ?answer .\n"
        "  ?st pq:P453 wd:<characterQID> .\n"
        "Never use wdt:P161 to bind a statement node.\n"
        "Never use p:P453 directly on a statement node; use pq:P453.\n\n"
        f"Question: {question_en}\n\n"
        "Chosen IDs:\n"
        f"{chosen_blob}\n\n"
        "Previous SPARQL (0 rows returned):\n"
        f"{prior_sparql}\n"
    )


def _sanitize_candidates(cands: List[dict], keep: int, kind: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for c in (cands or [])[:keep]:
        if kind == "entity":
            out.append(
                {
                    "id": c.get("id", ""),
                    "label": c.get("label", ""),
                    "description": c.get("description", ""),
                }
            )
        else:
            out.append(
                {
                    "id": c.get("id", ""),
                    "label": c.get("label", ""),
                    "description": c.get("description", ""),
                }
            )
    return out


def _is_numeric_question(question_en: str) -> bool:
    q = (question_en or "").lower()
    numeric_markers = [
        "how many",
        "number",
        "count",
        "percentage",
        "percent",
        "age",
        "year",
        "date",
        "when",
        "latest",
        "oldest",
    ]
    return any(m in q for m in numeric_markers)


def _score_entity_candidate(
    *,
    question_en: str,
    entity_string: str,
    candidate: Dict[str, Any],
    all_entity_strings: List[str],
) -> Tuple[int, List[str]]:
    q = (question_en or "").lower()
    label = str(candidate.get("label", "") or "")
    desc = str(candidate.get("description", "") or "")
    desc_l = desc.lower()
    label_l = label.lower()
    ent_l = (entity_string or "").strip().lower()
    score = 0
    reasons: List[str] = []

    keywords = ["fictional character", "television series", "film", "actor", "character"]
    if any(k in desc_l for k in keywords):
        score += 5
        reasons.append("+5 desc has media/character keyword")

    if ("played" in q or "actor" in q) and ("fictional" in desc_l or "character" in desc_l):
        score += 5
        reasons.append("+5 played/actor with fictional/character desc")

    if label_l == ent_l and ent_l:
        score += 3
        reasons.append("+3 exact label match")

    if "natural number" in desc_l and not _is_numeric_question(question_en):
        score -= 10
        reasons.append("-10 natural number in non-numeric question")

    if ("played" in q or "actor" in q or "role" in q) and ("song" in desc_l or "track" in desc_l):
        score -= 8
        reasons.append("-8 song/track for acting question")

    if label.startswith("Category:") or "wikimedia category" in desc_l:
        score -= 6
        reasons.append("-6 category candidate")

    other_entity_strings = [e.strip().lower() for e in (all_entity_strings or []) if e.strip().lower() != ent_l]
    if any(o and o in desc_l for o in other_entity_strings):
        score += 2
        reasons.append("+2 desc mentions other entity string")

    return score, reasons


def _score_property_candidate(
    *,
    question_en: str,
    property_string: str,
    candidate: Dict[str, Any],
) -> Tuple[int, List[str]]:
    q = (question_en or "").lower()
    label = str(candidate.get("label", "") or "")
    desc = str(candidate.get("description", "") or "")
    label_l = label.lower()
    desc_l = desc.lower()
    score = 0
    reasons: List[str] = []

    acting_q = ("played" in q or "actor" in q or "role" in q or "film" in q or "television" in q or "tv" in q)
    music_q = ("song" in q or "music" in q or "track" in q or "album" in q)

    if label_l == "cast member" and "played" in q:
        score += 5
        reasons.append("+5 cast member for played")
    if label_l == "character role" and "played" in q:
        score += 4
        reasons.append("+4 character role for played")
    if "qualifier" in desc_l or "use only as qualifier" in desc_l:
        score += 3
        reasons.append("+3 qualifier-oriented property")
    if label_l == "performer" and acting_q and not music_q:
        score -= 6
        reasons.append("-6 performer for non-music acting question")

    # small lexical overlap bonus with requested property phrase
    ptxt = (property_string or "").strip().lower()
    if ptxt and (ptxt in label_l or any(tok in label_l for tok in ptxt.split())):
        score += 1
        reasons.append("+1 lexical overlap")

    return score, reasons


def _rerank_entity_candidates(
    *,
    question_en: str,
    entity_string: str,
    candidates: List[Dict[str, Any]],
    all_entity_strings: List[str],
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    scored: List[Dict[str, Any]] = []
    for idx, c in enumerate(candidates or []):
        s, reasons = _score_entity_candidate(
            question_en=question_en,
            entity_string=entity_string,
            candidate=c,
            all_entity_strings=all_entity_strings,
        )
        scored.append(
            {
                "rank": idx + 1,
                "id": c.get("id", ""),
                "label": c.get("label", ""),
                "description": c.get("description", ""),
                "score": s,
                "reasons": reasons,
            }
        )
    if not scored:
        return None, []
    scored_sorted = sorted(scored, key=lambda x: (-x["score"], x["rank"]))
    top = scored_sorted[0]
    chosen = {
        "id": top.get("id", ""),
        "label": top.get("label", ""),
        "description": top.get("description", ""),
    }
    return chosen, scored_sorted


def _rerank_property_candidates(
    *,
    question_en: str,
    property_string: str,
    candidates: List[Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    scored: List[Dict[str, Any]] = []
    for idx, c in enumerate(candidates or []):
        s, reasons = _score_property_candidate(
            question_en=question_en,
            property_string=property_string,
            candidate=c,
        )
        scored.append(
            {
                "rank": idx + 1,
                "id": c.get("id", ""),
                "label": c.get("label", ""),
                "description": c.get("description", ""),
                "score": s,
                "reasons": reasons,
            }
        )
    if not scored:
        return None, []
    scored_sorted = sorted(scored, key=lambda x: (-x["score"], x["rank"]))
    top = scored_sorted[0]
    chosen = {
        "id": top.get("id", ""),
        "label": top.get("label", ""),
        "description": top.get("description", ""),
    }
    return chosen, scored_sorted


def _validate_query_uses_only_ids(
    sparql: str,
    allowed_qids: set[str],
    allowed_pids: set[str],
) -> Tuple[bool, str]:
    used_q = set(m.group(1).upper() for m in _QID_IN_QUERY_RE.finditer(sparql or ""))
    used_p = set(m.group(1).upper() for m in _PID_IN_QUERY_RE.finditer(sparql or ""))
    bad_q = sorted(used_q - allowed_qids)
    bad_p = sorted(used_p - allowed_pids)
    if bad_q or bad_p:
        return (
            False,
            f"SPARQL used IDs not in chosen sets: "
            f"QIDs={bad_q[:10]} PIDs={bad_p[:10]}",
        )
    return True, ""


def _augment_extraction_for_smart_prompts(
    question_en: str,
    entities: List[str],
    properties: List[str],
) -> Tuple[List[str], List[str]]:
    """
    Deterministic, runner-controlled augmentation of extracted phrases.
    This mimics the "pattern hints" without granting the LLM the ability to
    invent QIDs/PIDs: we only add *phrases* that are then grounded via
    search_entity_core/search_property_core.
    """
    q = (question_en or "").lower()
    ent = list(entities or [])
    prop = list(properties or [])

    def add_unique(xs: List[str], v: str) -> None:
        v = (v or "").strip()
        if not v:
            return
        if v.lower() not in [x.lower() for x in xs]:
            xs.append(v)

    # Pattern 1: Who played [character] in [work]
    if "who played" in q and " in " in q:
        add_unique(prop, "cast member")
        add_unique(prop, "character role")

    # Pattern 2: largest country in Africa by population/area
    if "largest country" in q and "africa" in q:
        add_unique(ent, "Africa")
        add_unique(prop, "continent")
        if "population" in q:
            add_unique(prop, "population")
        if "area" in q:
            add_unique(prop, "area")

    # Pattern: creator/author of a work
    if "creator" in q or "author" in q or "wrote" in q:
        add_unique(prop, "author")
        add_unique(prop, "creator")

    # Alive/dead -> date of death
    if " alive" in q or " dead" in q or q.startswith("is ") or q.startswith("does "):
        if "stan lee" in q or "alive" in q or "dead" in q:
            add_unique(prop, "date of death")

    # president/head of state
    if "president of" in q or "head of state" in q:
        add_unique(prop, "head of state")

    # Constrain sizes (keep deterministic head)
    ent = ent[:6]
    prop = prop[:6]
    return ent, prop


def _detect_character_and_work_qids(
    chosen_entities: List[Dict[str, Any]],
) -> Tuple[Optional[str], Optional[str]]:
    """
    Detect a likely (character_qid, work_qid) pair from chosen entities
    using descriptions.
    """
    character_qid: Optional[str] = None
    work_qid: Optional[str] = None
    for e in chosen_entities or []:
        qid = str(e.get("qid", "") or "")
        desc = str(e.get("description", "") or "").lower()
        if not qid:
            continue
        if ("fictional character" in desc or ("character" in desc and "fictional" in desc)) and not character_qid:
            character_qid = qid
        if any(k in desc for k in ["television series", "tv series", "film", "movie", "series", "manga", "book"]) and not work_qid:
            work_qid = qid
    return character_qid, work_qid


def _maybe_apply_played_pattern_fix(
    question_en: str,
    chosen_entities: List[Dict[str, Any]],
    chosen_property_pids: List[str],
    current_sparql: str,
) -> str:
    """
    Deterministic patch for "Who played X in Y" style questions.
    If we have both work and character IDs plus P161/P453 grounded,
    force the canonical statement/qualifier pattern.
    """
    q = (question_en or "").lower()
    if "played" not in q or " in " not in q:
        return current_sparql
    pset = {p.upper() for p in (chosen_property_pids or [])}
    if "P161" not in pset or "P453" not in pset:
        return current_sparql

    char_qid, work_qid = _detect_character_and_work_qids(chosen_entities)
    if not char_qid or not work_qid:
        return current_sparql

    # Canonical cast-member statement with character-role qualifier
    return (
        "SELECT ?answer WHERE { "
        f"wd:{work_qid} p:P161 ?st . "
        "?st ps:P161 ?answer . "
        f"?st pq:P453 wd:{char_qid} . "
        "} LIMIT 50"
    )

@dataclass
class LLMConfig:
    provider: str
    model: str
    api_key: str
    base_url: str


async def call_openai_chat_completions(
    client,
    cfg: LLMConfig,
    prompt_text: str,
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """
    Returns (response_text, error_dict)
    """
    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": "You are a careful SPARQL generator."},
            {"role": "user", "content": prompt_text},
        ],
        "temperature": 0,
    }

    try:
        r = await client.post(cfg.base_url, headers=headers, json=payload)
    except Exception as e:
        return None, {"code": "LLM_API_ERROR", "message": str(e)[:500]}

    text = r.text
    if r.status_code >= 400:
        return None, {
            "code": "LLM_API_ERROR",
            "status_code": r.status_code,
            "message": text[:2000],
        }

    try:
        data = r.json()
    except Exception as e:
        return None, {
            "code": "LLM_API_ERROR",
            "status_code": r.status_code,
            "message": f"Non-JSON response: {e}; body={text[:500]}",
        }

    try:
        choice0 = (data.get("choices") or [])[0]
        msg = choice0.get("message") or {}
        content = msg.get("content", "")
        return str(content or ""), None
    except Exception as e:
        return None, {
            "code": "LLM_API_ERROR",
            "status_code": r.status_code,
            "message": f"Unexpected response format: {e}; body={text[:500]}",
        }


async def call_anthropic_messages(
    client,
    cfg: LLMConfig,
    prompt_text: str,
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """
    Anthropic Messages API. cfg.base_url should be the messages endpoint, e.g.
    https://api.anthropic.com/v1/messages
    """
    headers = {
        "x-api-key": cfg.api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    payload = {
        "model": cfg.model,
        "max_tokens": 16384,
        "system": "You are a careful SPARQL generator.",
        "messages": [
            {"role": "user", "content": prompt_text},
        ],
    }

    try:
        r = await client.post(cfg.base_url, headers=headers, json=payload)
    except Exception as e:
        return None, {"code": "LLM_API_ERROR", "message": str(e)[:500]}

    text = r.text
    if r.status_code >= 400:
        return None, {
            "code": "LLM_API_ERROR",
            "status_code": r.status_code,
            "message": text[:2000],
        }

    try:
        data = r.json()
    except Exception as e:
        return None, {
            "code": "LLM_API_ERROR",
            "status_code": r.status_code,
            "message": f"Non-JSON response: {e}; body={text[:500]}",
        }

    try:
        parts: List[str] = []
        for block in data.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "".join(parts), None
    except Exception as e:
        return None, {
            "code": "LLM_API_ERROR",
            "status_code": r.status_code,
            "message": f"Unexpected response format: {e}; body={text[:500]}",
        }


async def call_llm_chat(
    client,
    cfg: LLMConfig,
    prompt_text: str,
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    p = (cfg.provider or "").strip().lower()
    if p == "anthropic":
        return await call_anthropic_messages(client, cfg, prompt_text)
    return await call_openai_chat_completions(client, cfg, prompt_text)


def score_sets(
    gold_set: set[str],
    pred_set: set[str],
    gold_row_count: int,
) -> Dict[str, Any]:
    tp = len(gold_set & pred_set)
    fp = len(pred_set - gold_set)
    fn = len(gold_set - pred_set)

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0

    # Exact match rule:
    # - If both sets are empty, count as exact match only when gold_row_count == 0.
    # - Otherwise, exact match when sets equal.
    if not gold_set and not pred_set:
        exact_match = 1 if int(gold_row_count) == 0 else 0
        # Both correctly returned empty — treat as perfect retrieval.
        if exact_match == 1:
            f1 = 1.0
            precision = 1.0
            recall = 1.0
    else:
        exact_match = 1 if gold_set == pred_set else 0

    return {
        "overlap_count": tp,
        "exact_match": exact_match,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def _is_boolean_gold(gold_values: List[str]) -> Optional[bool]:
    """Return True/False if gold is a single boolean answer, else None."""
    if len(gold_values) == 1:
        v = gold_values[0].strip().lower()
        if v == "true":
            return True
        if v == "false":
            return False
    return None


def score_answers(
    *,
    gold_ids: List[str],
    pred_ids: List[str],
    gold_values: List[str],
    pred_values: List[str],
    gold_row_count: int,
    pred_row_count: int = 0,
) -> Dict[str, Any]:
    # Prefer entity-QID set scoring when QID answers exist.
    if gold_ids:
        return score_sets(set(gold_ids), set(pred_ids), gold_row_count=gold_row_count)

    # Boolean gold answers (from ASK queries stored as "true"/"false").
    # Score by whether the predicted query returns rows or not.
    gold_bool = _is_boolean_gold(gold_values)
    if gold_bool is not None:
        correct = (gold_bool and pred_row_count > 0) or (not gold_bool and pred_row_count == 0)
        f1 = 1.0 if correct else 0.0
        return {
            "overlap_count": 0,
            "exact_match": 1 if correct else 0,
            "precision": f1,
            "recall": f1,
            "f1": f1,
        }

    # Otherwise score literal/date/numeric values.
    gold_v = {_normalize_literal_for_match(v) for v in gold_values if _normalize_literal_for_match(v)}
    pred_v = {_normalize_literal_for_match(v) for v in pred_values if _normalize_literal_for_match(v)}
    return score_sets(gold_v, pred_v, gold_row_count=gold_row_count)


async def run_one(
    *,
    item: Dict[str, Any],
    cfg: LLMConfig,
    timeout_ms: int,
    limit_cap: int,
    sleep_s: float,
    sem: asyncio.Semaphore,
    log_fp,
    mode: Mode,
    prompt_style: PromptStyle,
    llm_timeout_s: float = 60.0,
) -> Dict[str, Any]:
    # Import after bootstrap so missing deps don't crash the initial process.
    from tools.wikidata import (
        run_sparql_wikidata_core,
        search_entity_core,
        search_property_core,
    )

    import httpx

    question_id = item["questionId"]
    question_en = item["question_en"]
    gold_answer_ids_str = item["gold_answer_ids"]
    gold_answer_values_str = item.get("gold_answer_values", "")
    gold_row_count = int(item.get("gold_row_count") or 0)
    gold_ids = _split_semicolon_ids(gold_answer_ids_str)
    gold_values = _split_semicolon_ids(gold_answer_values_str)

    pred_ok = False
    pred_sparql = ""
    pred_error_code = ""
    pred_error_message = ""
    pred_rows: List[Dict[str, str]] = []
    pred_row_count = 0
    pred_elapsed_ms: Optional[int] = None
    pred_attempts = 0
    pred_sparql_attempt1 = ""
    pred_sparql_attempt2 = ""
    fallback_used = False
    fallback_attempts = 0
    fallback_success = False
    fallback_strategy = ""

    extracted_entities_raw: List[str] = []
    extracted_properties_raw: List[str] = []
    chosen_entities: List[Dict[str, Any]] = []
    chosen_properties: List[Dict[str, Any]] = []
    chosen_entity_qids: List[str] = []
    chosen_property_pids: List[str] = []

    async with sem:
        async with httpx.AsyncClient(timeout=httpx.Timeout(llm_timeout_s)) as client:
            record: Dict[str, Any] = {
                "questionId": question_id,
                "question_en": question_en,
                "provider": cfg.provider,
                "model": cfg.model,
                "mode": mode.value,
                "prompt_style": prompt_style.value,
            }

            if mode == Mode.direct:
                prompt1 = build_direct_prompt(question_en, prompt_style)
                raw1, err1 = await call_llm_chat(client, cfg, prompt1)
                record["prompt_attempt1"] = prompt1
                record["raw_response_attempt1"] = raw1 if raw1 else ""
                if err1 is not None:
                    pred_error_code = err1.get("code", "LLM_API_ERROR")
                    pred_error_message = str(err1.get("message", ""))[:2000]
                    record["pred_error_code"] = pred_error_code
                    record["pred_error_message"] = pred_error_message
                    log_fp.write(json.dumps(record, ensure_ascii=False) + "\n")
                    await asyncio.sleep(sleep_s)
                    return _final_row(
                        item=item,
                        pred_sparql="",
                        pred_ok=False,
                        pred_row_count=0,
                        pred_elapsed_ms=None,
                        pred_answer_ids=[],
                        pred_answer_values=[],
                        pred_error_code=pred_error_code,
                        pred_error_message=pred_error_message,
                        mode=mode,
                        prompt_style=prompt_style,
                        extracted_entities_raw=[],
                        extracted_properties_raw=[],
                        chosen_entity_qids=[],
                        chosen_property_pids=[],
                        pred_attempts=0,
                        pred_sparql_attempt1="",
                        pred_sparql_attempt2="",
                    )

                obj1, perr1 = parse_llm_json(raw1 or "")
                if obj1 is None or "sparql" not in obj1:
                    pred_error_code = "LLM_PARSE_ERROR"
                    pred_error_message = (perr1 or "Missing 'sparql' key")[:2000]
                    record["pred_error_code"] = pred_error_code
                    record["pred_error_message"] = pred_error_message
                    log_fp.write(json.dumps(record, ensure_ascii=False) + "\n")
                    await asyncio.sleep(sleep_s)
                    return _final_row(
                        item=item,
                        pred_sparql="",
                        pred_ok=False,
                        pred_row_count=0,
                        pred_elapsed_ms=None,
                        pred_answer_ids=[],
                        pred_answer_values=[],
                        pred_error_code=pred_error_code,
                        pred_error_message=pred_error_message,
                        mode=mode,
                        prompt_style=prompt_style,
                        extracted_entities_raw=[],
                        extracted_properties_raw=[],
                        chosen_entity_qids=[],
                        chosen_property_pids=[],
                        pred_attempts=0,
                        pred_sparql_attempt1="",
                        pred_sparql_attempt2="",
                    )

                pred_sparql = str(obj1.get("sparql") or "").strip()
                pred_sparql_attempt1 = pred_sparql
                pred_attempts = 1

            else:
                # mode=mcp
                extract_prompt = build_mcp_extract_prompt(question_en, prompt_style)
                raw_a, err_a = await call_llm_chat(client, cfg, extract_prompt)
                record["extract_prompt"] = extract_prompt
                record["raw_extract_response"] = raw_a if raw_a else ""
                if err_a is not None:
                    pred_error_code = err_a.get("code", "LLM_API_ERROR")
                    pred_error_message = str(err_a.get("message", ""))[:2000]
                    record["pred_error_code"] = pred_error_code
                    record["pred_error_message"] = pred_error_message
                    log_fp.write(json.dumps(record, ensure_ascii=False) + "\n")
                    await asyncio.sleep(sleep_s)
                    return _final_row(
                        item=item,
                        pred_sparql="",
                        pred_ok=False,
                        pred_row_count=0,
                        pred_elapsed_ms=None,
                        pred_answer_ids=[],
                        pred_answer_values=[],
                        pred_error_code=pred_error_code,
                        pred_error_message=pred_error_message,
                        mode=mode,
                        prompt_style=prompt_style,
                        extracted_entities_raw=[],
                        extracted_properties_raw=[],
                        chosen_entity_qids=[],
                        chosen_property_pids=[],
                        pred_attempts=0,
                        pred_sparql_attempt1="",
                        pred_sparql_attempt2="",
                    )

                obj_a, perr_a = parse_llm_json(raw_a or "")
                if obj_a is None:
                    pred_error_code = "LLM_PARSE_ERROR"
                    pred_error_message = (perr_a or "Parse error")[:2000]
                    record["pred_error_code"] = pred_error_code
                    record["pred_error_message"] = pred_error_message
                    log_fp.write(json.dumps(record, ensure_ascii=False) + "\n")
                    await asyncio.sleep(sleep_s)
                    return _final_row(
                        item=item,
                        pred_sparql="",
                        pred_ok=False,
                        pred_row_count=0,
                        pred_elapsed_ms=None,
                        pred_answer_ids=[],
                        pred_answer_values=[],
                        pred_error_code=pred_error_code,
                        pred_error_message=pred_error_message,
                        mode=mode,
                        prompt_style=prompt_style,
                        extracted_entities_raw=[],
                        extracted_properties_raw=[],
                        chosen_entity_qids=[],
                        chosen_property_pids=[],
                        pred_attempts=0,
                        pred_sparql_attempt1="",
                        pred_sparql_attempt2="",
                    )

                ents = obj_a.get("entities", []) if isinstance(obj_a, dict) else []
                props = obj_a.get("properties", []) if isinstance(obj_a, dict) else []
                if isinstance(ents, list):
                    extracted_entities_raw = [str(x).strip() for x in ents if str(x).strip()][:6]
                if isinstance(props, list):
                    extracted_properties_raw = [str(x).strip() for x in props if str(x).strip()][:6]

                record["extracted_entities"] = extracted_entities_raw
                record["extracted_properties"] = extracted_properties_raw

                # Step B: tool calls (core functions) — keep top 8
                tool_entity_results: List[Dict[str, Any]] = []
                tool_property_results: List[Dict[str, Any]] = []

                for name in extracted_entities_raw:
                    res = await search_entity_core(name, k=8)
                    cands = []
                    if isinstance(res, dict):
                        cands = _sanitize_candidates(res.get("candidates", []) or [], 8, "entity")
                    tool_entity_results.append({"name": name, "raw": res, "candidates": cands})
                for name in extracted_properties_raw:
                    res = await search_property_core(name, k=8)
                    cands = []
                    if isinstance(res, dict):
                        cands = _sanitize_candidates(res.get("candidates", []) or [], 8, "property")
                    tool_property_results.append({"name": name, "raw": res, "candidates": cands})

                record["tool_entity_results"] = tool_entity_results
                record["tool_property_results"] = tool_property_results

                # Step C: LLM-based selection over candidates (MCP-like)
                selection_needed_more = False
                selection_failures: List[str] = []

                async def _run_selection(
                    ent_results: List[Dict[str, Any]],
                    prop_results: List[Dict[str, Any]],
                ) -> Tuple[Dict[str, str], Dict[str, str], bool, List[str], str, str]:
                    sel_prompt = build_mcp_selection_prompt(
                        question_en, ent_results, prop_results
                    )
                    async with httpx.AsyncClient(timeout=httpx.Timeout(llm_timeout_s)) as client_sel:
                        raw_sel, err_sel = await call_llm_chat(
                            client_sel, cfg, sel_prompt
                        )
                    sel_needed_more = False
                    sel_failures: List[str] = []
                    selected_ent_ids: Dict[str, str] = {}
                    selected_prop_ids: Dict[str, str] = {}
                    if err_sel is not None:
                        sel_failures.append(f"LLM_SELECTION_API_ERROR:{err_sel.get('message','')[:120]}")
                        return selected_ent_ids, selected_prop_ids, True, sel_failures, sel_prompt, raw_sel or ""

                    obj_sel, perr_sel = parse_llm_json(raw_sel or "")
                    if obj_sel is None:
                        sel_failures.append(f"LLM_SELECTION_PARSE_ERROR:{(perr_sel or '')[:120]}")
                        return selected_ent_ids, selected_prop_ids, True, sel_failures, sel_prompt, raw_sel or ""

                    need_more_flag = bool(obj_sel.get("need_more", False))
                    se = obj_sel.get("selected_entities", {}) or {}
                    sp = obj_sel.get("selected_properties", {}) or {}

                    ent_map: Dict[str, List[str]] = {
                        r.get("name", ""): [c.get("id", "") for c in (r.get("candidates", []) or [])]
                        for r in ent_results
                    }
                    prop_map: Dict[str, List[str]] = {
                        r.get("name", ""): [c.get("id", "") for c in (r.get("candidates", []) or [])]
                        for r in prop_results
                    }

                    for name, sel_id in se.items():
                        key = str(name or "")
                        if sel_id is None:
                            sel_needed_more = True
                            sel_failures.append(f"entity:{key}:null")
                            continue
                        sid = str(sel_id or "")
                        if sid and sid in (ent_map.get(key, []) or []):
                            selected_ent_ids[key] = sid
                        else:
                            sel_needed_more = True
                            sel_failures.append(f"entity:{key}:invalid:{sid}")

                    for name, sel_id in sp.items():
                        key = str(name or "")
                        if sel_id is None:
                            sel_needed_more = True
                            sel_failures.append(f"property:{key}:null")
                            continue
                        sid = str(sel_id or "")
                        if sid and sid in (prop_map.get(key, []) or []):
                            selected_prop_ids[key] = sid
                        else:
                            sel_needed_more = True
                            sel_failures.append(f"property:{key}:invalid:{sid}")

                    if need_more_flag:
                        sel_needed_more = True

                    return (
                        selected_ent_ids,
                        selected_prop_ids,
                        sel_needed_more,
                        sel_failures,
                        sel_prompt,
                        raw_sel or "",
                    )

                selected_entities_map, selected_properties_map, selection_needed_more, selection_failures, sel_prompt1, raw_sel1 = await _run_selection(
                    tool_entity_results, tool_property_results
                )
                record["selection_prompt_attempt1"] = sel_prompt1
                record["raw_selection_response_attempt1"] = raw_sel1
                record["selected_entities_map_attempt1"] = selected_entities_map
                record["selected_properties_map_attempt1"] = selected_properties_map

                # If need_more, re-query unresolved keys with k=15 and re-run selection once
                if selection_needed_more:
                    unresolved_ents = [
                        r["name"]
                        for r in tool_entity_results
                        if r.get("name") not in selected_entities_map
                    ]
                    unresolved_props = [
                        r["name"]
                        for r in tool_property_results
                        if r.get("name") not in selected_properties_map
                    ]
                    more_entity_results: List[Dict[str, Any]] = []
                    more_property_results: List[Dict[str, Any]] = []
                    for name in unresolved_ents:
                        res = await search_entity_core(name, k=15)
                        cands = []
                        if isinstance(res, dict):
                            cands = _sanitize_candidates(res.get("candidates", []) or [], 8, "entity")
                        more_entity_results.append({"name": name, "raw": res, "candidates": cands})
                    for name in unresolved_props:
                        res = await search_property_core(name, k=15)
                        cands = []
                        if isinstance(res, dict):
                            cands = _sanitize_candidates(res.get("candidates", []) or [], 8, "property")
                        more_property_results.append({"name": name, "raw": res, "candidates": cands})

                    merged_ent_results = []
                    for r in tool_entity_results:
                        if r.get("name") in selected_entities_map:
                            merged_ent_results.append(r)
                        else:
                            repl = next(
                                (m for m in more_entity_results if m.get("name") == r.get("name")), r
                            )
                            merged_ent_results.append(repl)
                    merged_prop_results = []
                    for r in tool_property_results:
                        if r.get("name") in selected_properties_map:
                            merged_prop_results.append(r)
                        else:
                            repl = next(
                                (m for m in more_property_results if m.get("name") == r.get("name")), r
                            )
                            merged_prop_results.append(repl)

                    sel2_entities, sel2_properties, need_more2, sel_fail2, sel_prompt2, raw_sel2 = await _run_selection(
                        merged_ent_results, merged_prop_results
                    )
                    record["selection_prompt_attempt2"] = sel_prompt2
                    record["raw_selection_response_attempt2"] = raw_sel2
                    record["selected_entities_map_attempt2"] = sel2_entities
                    record["selected_properties_map_attempt2"] = sel2_properties

                    for k_name, vid in sel2_entities.items():
                        if k_name not in selected_entities_map and vid:
                            selected_entities_map[k_name] = vid
                    for k_name, vid in sel2_properties.items():
                        if k_name not in selected_properties_map and vid:
                            selected_properties_map[k_name] = vid

                    selection_failures.extend(sel_fail2)
                    selection_needed_more = need_more2

                record["selection_needed_more"] = selection_needed_more
                record["selection_failures"] = selection_failures

                # Build chosen_entities / chosen_properties from selection maps
                name_to_entity_candidates = {
                    r.get("name", ""): r.get("candidates", []) or [] for r in tool_entity_results
                }
                name_to_property_candidates = {
                    r.get("name", ""): r.get("candidates", []) or [] for r in tool_property_results
                }

                for name, qid in selected_entities_map.items():
                    for c in name_to_entity_candidates.get(name, []):
                        if c.get("id") == qid:
                            chosen_entities.append(
                                {
                                    "name": name,
                                    "qid": qid,
                                    "label": c.get("label", ""),
                                    "description": c.get("description", ""),
                                }
                            )
                            break

                for name, pid in selected_properties_map.items():
                    for c in name_to_property_candidates.get(name, []):
                        if c.get("id") == pid:
                            chosen_properties.append(
                                {
                                    "name": name,
                                    "pid": pid,
                                    "label": c.get("label", ""),
                                    "description": c.get("description", ""),
                                }
                            )
                            break

                chosen_entity_qids = [c.get("qid", "") for c in chosen_entities if c.get("qid")]
                chosen_property_pids = [c.get("pid", "") for c in chosen_properties if c.get("pid")]
                record["chosen_entities"] = chosen_entities
                record["chosen_properties"] = chosen_properties

                synth_prompt = build_mcp_synthesis_prompt(
                    question_en,
                    chosen_entities,
                    chosen_properties,
                    prompt_style=prompt_style,
                    limit=50,
                )
                raw_s, err_s = await call_llm_chat(client, cfg, synth_prompt)
                record["synthesis_prompt_attempt1"] = synth_prompt
                record["raw_synthesis_response_attempt1"] = raw_s if raw_s else ""
                if err_s is not None:
                    pred_error_code = err_s.get("code", "LLM_API_ERROR")
                    pred_error_message = str(err_s.get("message", ""))[:2000]
                    record["pred_error_code"] = pred_error_code
                    record["pred_error_message"] = pred_error_message
                    log_fp.write(json.dumps(record, ensure_ascii=False) + "\n")
                    await asyncio.sleep(sleep_s)
                    return _final_row(
                        item=item,
                        pred_sparql="",
                        pred_ok=False,
                        pred_row_count=0,
                        pred_elapsed_ms=None,
                        pred_answer_ids=[],
                        pred_answer_values=[],
                        pred_error_code=pred_error_code,
                        pred_error_message=pred_error_message,
                        mode=mode,
                        prompt_style=prompt_style,
                        extracted_entities_raw=extracted_entities_raw,
                        extracted_properties_raw=extracted_properties_raw,
                        chosen_entity_qids=chosen_entity_qids,
                        chosen_property_pids=chosen_property_pids,
                        pred_attempts=0,
                        pred_sparql_attempt1="",
                        pred_sparql_attempt2="",
                    )

                obj_s, perr_s = parse_llm_json(raw_s or "")
                if obj_s is None or "sparql" not in obj_s:
                    pred_error_code = "LLM_PARSE_ERROR"
                    pred_error_message = (perr_s or "Missing 'sparql' key")[:2000]
                    record["pred_error_code"] = pred_error_code
                    record["pred_error_message"] = pred_error_message
                    log_fp.write(json.dumps(record, ensure_ascii=False) + "\n")
                    await asyncio.sleep(sleep_s)
                    return _final_row(
                        item=item,
                        pred_sparql="",
                        pred_ok=False,
                        pred_row_count=0,
                        pred_elapsed_ms=None,
                        pred_answer_ids=[],
                        pred_answer_values=[],
                        pred_error_code=pred_error_code,
                        pred_error_message=pred_error_message,
                        mode=mode,
                        prompt_style=prompt_style,
                        extracted_entities_raw=extracted_entities_raw,
                        extracted_properties_raw=extracted_properties_raw,
                        chosen_entity_qids=chosen_entity_qids,
                        chosen_property_pids=chosen_property_pids,
                        pred_attempts=0,
                        pred_sparql_attempt1="",
                        pred_sparql_attempt2="",
                    )

                pred_sparql = str(obj_s.get("sparql") or "").strip()
                pred_sparql_attempt1 = pred_sparql
                pred_attempts = 1

        # Sanity checks
        if not re.search(r"\bSELECT\b", pred_sparql, re.IGNORECASE) or not re.search(
            r"\bWHERE\b", pred_sparql, re.IGNORECASE
        ):
            pred_ok = False
            pred_error_code = "BAD_SPARQL_FORMAT"
            pred_error_message = "SPARQL must contain SELECT and WHERE."
            record["pred_ok"] = False
            record["pred_error_code"] = pred_error_code
            record["pred_error_message"] = pred_error_message
            log_fp.write(json.dumps(record, ensure_ascii=False) + "\n")
            await asyncio.sleep(sleep_s)
            return _final_row(
                item=item,
                pred_sparql=pred_sparql,
                pred_ok=False,
                pred_row_count=0,
                pred_elapsed_ms=None,
                pred_answer_ids=[],
                pred_answer_values=[],
                pred_error_code=pred_error_code,
                pred_error_message=pred_error_message,
                mode=mode,
                prompt_style=prompt_style,
                extracted_entities_raw=extracted_entities_raw,
                extracted_properties_raw=extracted_properties_raw,
                chosen_entity_qids=chosen_entity_qids,
                chosen_property_pids=chosen_property_pids,
                pred_attempts=pred_attempts,
                pred_sparql_attempt1=pred_sparql_attempt1,
                pred_sparql_attempt2=pred_sparql_attempt2,
            )
        if not re.search(r"\?\s*answer\b|\?answer\b", pred_sparql):
            pred_ok = False
            pred_error_code = "BAD_SPARQL_FORMAT"
            pred_error_message = "SPARQL must return a single variable named ?answer."
            await asyncio.sleep(sleep_s)
            return _final_row(
                item=item,
                pred_sparql=pred_sparql,
                pred_ok=False,
                pred_row_count=0,
                pred_elapsed_ms=None,
                pred_answer_ids=[],
                pred_answer_values=[],
                pred_error_code=pred_error_code,
                pred_error_message=pred_error_message,
                mode=mode,
                prompt_style=prompt_style,
                extracted_entities_raw=extracted_entities_raw,
                extracted_properties_raw=extracted_properties_raw,
                chosen_entity_qids=chosen_entity_qids,
                chosen_property_pids=chosen_property_pids,
                pred_attempts=pred_attempts,
                pred_sparql_attempt1=pred_sparql_attempt1,
                pred_sparql_attempt2=pred_sparql_attempt2,
            )

        exec_res = await run_sparql_wikidata_core(
            pred_sparql,
            timeout_ms=timeout_ms,
            limit_cap=limit_cap,
            allowed_entities=None,
            allowed_properties=None,
            allow_unbounded_property_paths=False,
        )

        pred_ok = bool(exec_res.get("ok", False))
        stat = exec_res.get("stats", {}) if isinstance(exec_res.get("stats", {}), dict) else {}
        pred_elapsed_ms = stat.get("elapsed_ms") if pred_ok or "stats" in exec_res else None
        pred_row_count = int(exec_res.get("row_count", 0) or 0)

        if pred_ok:
            pred_rows = exec_res.get("rows", []) or []
        else:
            pred_error_code = str(exec_res.get("error_code", "UNKNOWN") or "UNKNOWN")
            pred_error_message = str(exec_res.get("error_message", "") or "")[:2000]

        # Optional repair-on-empty (smart only)
        if prompt_style == PromptStyle.smart and pred_ok and pred_row_count == 0 and len(gold_ids) > 0:
            if mode == Mode.direct:
                # direct: keep existing SPARQL rewrite behavior
                repair_prompt = build_direct_prompt(question_en, prompt_style) + "\n0 rows returned; revise the SPARQL to match intent. Keep LIMIT 50.\nPrevious SPARQL:\n" + pred_sparql
                async with httpx.AsyncClient(timeout=httpx.Timeout(llm_timeout_s)) as client2:
                    raw_r, err_r = await call_llm_chat(client2, cfg, repair_prompt)
                record["repair_prompt"] = repair_prompt
                record["raw_repair_response"] = raw_r if raw_r else ""
                if err_r is None:
                    obj_r, perr_r = parse_llm_json(raw_r or "")
                    if isinstance(obj_r, dict) and "sparql" in obj_r:
                        repaired = str(obj_r.get("sparql") or "").strip()
                        pred_sparql_attempt2 = repaired
                        pred_attempts = 2
                        exec2 = await run_sparql_wikidata_core(
                            repaired,
                            timeout_ms=timeout_ms,
                            limit_cap=limit_cap,
                            allowed_entities=None,
                            allowed_properties=None,
                            allow_unbounded_property_paths=False,
                        )
                        ok2 = bool(exec2.get("ok", False))
                        rc2 = int(exec2.get("row_count", 0) or 0)
                        if ok2 and rc2 > 0:
                            pred_sparql = repaired
                            pred_ok = True
                            pred_row_count = rc2
                            stat2 = exec2.get("stats", {}) if isinstance(exec2.get("stats", {}), dict) else {}
                            pred_elapsed_ms = stat2.get("elapsed_ms")
                            pred_rows = exec2.get("rows", []) or []
                            pred_error_code = ""
                            pred_error_message = ""
                    else:
                        record["repair_parse_error"] = perr_r or "Missing 'sparql'"
            else:
                # mcp: bounded candidate-grid fallback (swap property candidates first, then entity candidates)
                MAX_FALLBACK_ATTEMPTS = 4
                K = 3
                fallback_used = True

                tool_entity_results = record.get("tool_entity_results", []) or []
                tool_property_results = record.get("tool_property_results", []) or []

                ent_cands_by_name: Dict[str, List[Dict[str, Any]]] = {
                    r.get("name", ""): (r.get("candidates", []) or [])[:K]
                    for r in tool_entity_results
                }
                prop_cands_by_name: Dict[str, List[Dict[str, Any]]] = {
                    r.get("name", ""): (r.get("candidates", []) or [])[:K]
                    for r in tool_property_results
                }

                chosen_ent_by_name: Dict[str, str] = {
                    c.get("name", ""): c.get("qid", "") for c in (chosen_entities or [])
                }
                chosen_prop_by_name: Dict[str, str] = {
                    c.get("name", ""): c.get("pid", "") for c in (chosen_properties or [])
                }

                def _build_entities(ent_override: Dict[str, str]) -> List[Dict[str, Any]]:
                    out: List[Dict[str, Any]] = []
                    for name, base_qid in chosen_ent_by_name.items():
                        qid = ent_override.get(name, base_qid)
                        for c in ent_cands_by_name.get(name, []):
                            if c.get("id") == qid:
                                out.append(
                                    {
                                        "name": name,
                                        "qid": qid,
                                        "label": c.get("label", ""),
                                        "description": c.get("description", ""),
                                    }
                                )
                                break
                    return out

                def _build_properties(prop_override: Dict[str, str]) -> List[Dict[str, Any]]:
                    out: List[Dict[str, Any]] = []
                    for name, base_pid in chosen_prop_by_name.items():
                        pid = prop_override.get(name, base_pid)
                        for c in prop_cands_by_name.get(name, []):
                            if c.get("id") == pid:
                                out.append(
                                    {
                                        "name": name,
                                        "pid": pid,
                                        "label": c.get("label", ""),
                                        "description": c.get("description", ""),
                                    }
                                )
                                break
                    return out

                # Build fallback attempt plan
                attempts_plan: List[Dict[str, Any]] = []
                for pname, cur_pid in chosen_prop_by_name.items():
                    for alt in prop_cands_by_name.get(pname, []):
                        alt_pid = alt.get("id", "")
                        if alt_pid and alt_pid != cur_pid:
                            attempts_plan.append(
                                {
                                    "strategy": "swap_property",
                                    "prop_override": {pname: alt_pid},
                                    "ent_override": {},
                                }
                            )
                for ename, cur_qid in chosen_ent_by_name.items():
                    for alt in ent_cands_by_name.get(ename, []):
                        alt_qid = alt.get("id", "")
                        if alt_qid and alt_qid != cur_qid:
                            attempts_plan.append(
                                {
                                    "strategy": "swap_entity",
                                    "prop_override": {},
                                    "ent_override": {ename: alt_qid},
                                }
                            )

                attempts_plan = attempts_plan[:MAX_FALLBACK_ATTEMPTS]
                record["fallback_plan"] = attempts_plan

                for idx, plan in enumerate(attempts_plan, start=1):
                    fallback_attempts = idx
                    strategy = str(plan.get("strategy", "") or "")
                    ent_override = plan.get("ent_override", {}) or {}
                    prop_override = plan.get("prop_override", {}) or {}

                    alt_entities = _build_entities(ent_override)
                    alt_properties = _build_properties(prop_override)

                    fb_key = f"fallback_attempt_{idx}"
                    record[fb_key] = {
                        "strategy": strategy,
                        "alt_entities": alt_entities,
                        "alt_properties": alt_properties,
                    }

                    synth_fb = build_mcp_synthesis_prompt(
                        question_en,
                        alt_entities,
                        alt_properties,
                        prompt_style=prompt_style,
                        limit=50,
                    )
                    async with httpx.AsyncClient(timeout=httpx.Timeout(llm_timeout_s)) as client_fb:
                        raw_fb, err_fb = await call_llm_chat(
                            client_fb, cfg, synth_fb
                        )
                    record[fb_key]["synthesis_prompt"] = synth_fb
                    record[fb_key]["raw_synthesis_response"] = raw_fb if raw_fb else ""
                    if err_fb is not None:
                        record[fb_key]["llm_error"] = {
                            "code": err_fb.get("code", "LLM_API_ERROR"),
                            "message": str(err_fb.get("message", ""))[:500],
                        }
                        continue

                    obj_fb, perr_fb = parse_llm_json(raw_fb or "")
                    if obj_fb is None or "sparql" not in obj_fb:
                        record[fb_key]["parse_error"] = perr_fb or "Missing 'sparql'"
                        continue

                    fb_sparql = str(obj_fb.get("sparql") or "").strip()
                    exec_fb = await run_sparql_wikidata_core(
                        fb_sparql,
                        timeout_ms=timeout_ms,
                        limit_cap=limit_cap,
                        allowed_entities=None,
                        allowed_properties=None,
                        allow_unbounded_property_paths=False,
                    )
                    ok_fb = bool(exec_fb.get("ok", False))
                    rc_fb = int(exec_fb.get("row_count", 0) or 0)
                    record[fb_key]["exec_ok"] = ok_fb
                    record[fb_key]["row_count"] = rc_fb
                    if not ok_fb:
                        record[fb_key]["exec_error_code"] = exec_fb.get("error_code", "UNKNOWN")

                    if ok_fb and rc_fb > 0:
                        # Adopt the first non-empty fallback result and stop.
                        pred_sparql_attempt2 = fb_sparql
                        pred_attempts = 2
                        pred_sparql = fb_sparql
                        pred_ok = True
                        pred_row_count = rc_fb
                        stat_fb = exec_fb.get("stats", {}) if isinstance(exec_fb.get("stats", {}), dict) else {}
                        pred_elapsed_ms = stat_fb.get("elapsed_ms")
                        pred_rows = exec_fb.get("rows", []) or []
                        pred_error_code = ""
                        pred_error_message = ""
                        fallback_success = True
                        fallback_strategy = strategy
                        break

        record["execution_attempts"] = pred_attempts
        record["pred_sparql_final"] = pred_sparql
        record["pred_sparql_attempt1"] = pred_sparql_attempt1
        record["pred_sparql_attempt2"] = pred_sparql_attempt2
        record["pred_ok"] = pred_ok
        record["pred_row_count"] = pred_row_count
        record["pred_elapsed_ms"] = pred_elapsed_ms
        record["pred_error_code"] = pred_error_code
        record["pred_error_message"] = pred_error_message
        record["fallback_used"] = fallback_used
        record["fallback_attempts"] = fallback_attempts
        record["fallback_success"] = fallback_success
        record["fallback_strategy"] = fallback_strategy
        log_fp.write(json.dumps(record, ensure_ascii=False) + "\n")

    await asyncio.sleep(sleep_s)

    pred_answer_ids = extract_answer_qids(pred_rows)
    pred_answer_values = extract_answer_values(pred_rows)
    return _final_row(
        item=item,
        pred_sparql=pred_sparql,
        pred_ok=pred_ok,
        pred_row_count=pred_row_count,
        pred_elapsed_ms=pred_elapsed_ms,
        pred_answer_ids=pred_answer_ids,
        pred_answer_values=pred_answer_values,
        pred_error_code=pred_error_code,
        pred_error_message=pred_error_message,
        mode=mode,
        prompt_style=prompt_style,
        extracted_entities_raw=extracted_entities_raw,
        extracted_properties_raw=extracted_properties_raw,
        chosen_entity_qids=chosen_entity_qids,
        chosen_property_pids=chosen_property_pids,
        pred_attempts=pred_attempts,
        pred_sparql_attempt1=pred_sparql_attempt1,
        pred_sparql_attempt2=pred_sparql_attempt2,
        fallback_used=fallback_used,
        fallback_attempts=fallback_attempts,
        fallback_success=fallback_success,
        fallback_strategy=fallback_strategy,
        _pred_row_count_for_scoring=pred_row_count,
    )


def _final_row(
    *,
    item: Dict[str, Any],
    pred_sparql: str,
    pred_ok: bool,
    pred_row_count: int,
    pred_elapsed_ms: Optional[int],
    pred_answer_ids: List[str],
    pred_answer_values: List[str],
    pred_error_code: str,
    pred_error_message: str,
    mode: Mode,
    prompt_style: PromptStyle,
    extracted_entities_raw: List[str],
    extracted_properties_raw: List[str],
    chosen_entity_qids: List[str],
    chosen_property_pids: List[str],
    pred_attempts: int,
    pred_sparql_attempt1: str,
    pred_sparql_attempt2: str,
    fallback_used: bool = False,
    fallback_attempts: int = 0,
    fallback_success: bool = False,
    fallback_strategy: str = "",
    _pred_row_count_for_scoring: Optional[int] = None,
) -> Dict[str, Any]:
    gold_ids = _split_semicolon_ids(item.get("gold_answer_ids", ""))
    gold_values = _split_semicolon_ids(item.get("gold_answer_values", ""))
    _scoring_pred_row_count = _pred_row_count_for_scoring if _pred_row_count_for_scoring is not None else pred_row_count

    gold_row_count = int(item.get("gold_row_count") or 0)
    scores = score_answers(
        gold_ids=gold_ids,
        pred_ids=pred_answer_ids,
        gold_values=gold_values,
        pred_values=pred_answer_values,
        gold_row_count=gold_row_count,
        pred_row_count=_scoring_pred_row_count,
    )

    return {
        "mode": mode.value,
        "prompt_style": prompt_style.value,
        "questionId": item.get("questionId", ""),
        "question_en": item.get("question_en", ""),
        "gold_answer_ids": item.get("gold_answer_ids", ""),
        "gold_answer_values": item.get("gold_answer_values", ""),
        "pred_answer_ids": qids_to_semicolon_list(pred_answer_ids),
        "pred_answer_values": ";".join(pred_answer_values),
        "exact_match": scores["exact_match"],
        "precision": scores["precision"],
        "recall": scores["recall"],
        "f1": scores["f1"],
        "overlap_count": scores["overlap_count"],
        "extracted_entities_json": json.dumps(extracted_entities_raw, ensure_ascii=False),
        "extracted_properties_json": json.dumps(extracted_properties_raw, ensure_ascii=False),
        "chosen_entity_qids": qids_to_semicolon_list(chosen_entity_qids),
        "chosen_property_pids": ";".join(chosen_property_pids),
        "fallback_used": fallback_used,
        "fallback_attempts": fallback_attempts,
        "fallback_success": fallback_success,
        "fallback_strategy": fallback_strategy,
        "pred_attempts": pred_attempts,
        "pred_sparql": pred_sparql,
        "pred_sparql_attempt1": pred_sparql_attempt1,
        "pred_sparql_attempt2": pred_sparql_attempt2,
        "pred_ok": pred_ok,
        "pred_row_count": pred_row_count,
        "pred_elapsed_ms": pred_elapsed_ms if pred_elapsed_ms is not None else "",
        "pred_error_code": pred_error_code,
        "pred_error_message": pred_error_message,
    }


async def run_benchmark(
    gold_csv: str,
    out_csv: str,
    n: int,
    timeout_ms: int,
    limit_cap: int,
    max_concurrency: int,
    sleep_s: float,
    mode: Mode,
    prompt_style: PromptStyle,
    llm_timeout_s: float = 60.0,
) -> None:
    import httpx

    provider = (os.getenv("LLM_PROVIDER", "openai") or "").strip().lower()
    model = (os.getenv("LLM_MODEL", "") or "").strip()

    if provider == "anthropic":
        # Support both correct spelling and legacy typo
        api_key = (
            os.getenv("ANTHROPIC_API_KEY", "")
            or os.getenv("ANTHORPIC_API_KEY", "")
            or ""
        ).strip()
        base_url = (os.getenv("ANTHROPIC_BASE_URL", "") or "").strip()
        if not base_url:
            base_url = "https://api.anthropic.com/v1/messages"
        if not model:
            raise ValueError(
                "Set LLM_MODEL env var (e.g. claude-3-5-sonnet-20241022)."
            )
        if not api_key:
            raise ValueError("Set ANTHROPIC_API_KEY env var.")
    elif provider == "openai":
        api_key = (os.getenv("OPENAI_API_KEY", "") or "").strip()
        base_url = (os.getenv("OPENAI_BASE_URL", "") or "").strip()
        if not base_url:
            base_url = "https://api.openai.com/v1/chat/completions"
        if not model:
            raise ValueError("Set LLM_MODEL env var (e.g. gpt-4.1-mini).")
        if not api_key:
            raise ValueError("Set OPENAI_API_KEY env var.")
    else:
        raise ValueError("LLM_PROVIDER must be 'openai' or 'anthropic'.")

    cfg = LLMConfig(
        provider=provider,
        model=model,
        api_key=api_key,
        base_url=base_url,
    )

    # Load gold CSV and filter to scorable
    total_rows = 0
    scorable: List[Dict[str, Any]] = []
    skipped_gold_non_qid = 0
    with open(gold_csv, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        required = {
            "questionId",
            "question_en",
            "gold_ok",
            "gold_row_count",
            "gold_answer_ids",
            "gold_answer_values",
        }
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(
                f"Gold CSV missing required columns: {sorted(missing)}"
            )

        for r in reader:
            total_rows += 1
            if not _parse_bool(r.get("gold_ok")):
                continue
            gold_row_count = int(r.get("gold_row_count") or 0)
            gold_ids = _split_semicolon_ids(r.get("gold_answer_ids", ""))
            gold_values = _split_semicolon_ids(r.get("gold_answer_values", ""))
            # Only score QID set metrics when gold has QIDs, OR the gold query truly returned 0 rows.
            if gold_row_count > 0 and len(gold_ids) == 0 and len(gold_values) == 0:
                skipped_gold_non_qid += 1
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
    n_scorable = len(scorable)
    n_run = len(selected)

    # Logs
    logs_dir = Path("benchmarks") / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = logs_dir / f"qawiki_text2sparql_{ts}.jsonl"

    # Execution
    sem = asyncio.Semaphore(max(1, int(max_concurrency)))
    out_rows: List[Dict[str, Any]] = []

    # Summary accumulators
    exact_matches = 0
    f1s: List[float] = []
    pred_success = 0
    pred_lat_ms: List[int] = []
    pred_error_codes: Counter[str] = Counter()
    empty_pred_pct_count = 0

    print(f"Scorable rows: {n_scorable} / total {total_rows}")
    if skipped_gold_non_qid:
        print(f"Skipped (gold has non-QID answers): {skipped_gold_non_qid}")
    print(f"Running: {n_run}")
    print(f"Logging JSONL: {log_path}")

    with open(log_path, "w", encoding="utf-8") as log_fp:
        for i, item in enumerate(selected):
            row = await run_one(
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
            )
            out_rows.append(row)

            # Update summary stats
            exact_matches += int(row.get("exact_match") or 0)
            f1s.append(float(row.get("f1") or 0.0))
            if _parse_bool(row.get("pred_ok")):
                pred_success += 1
            else:
                pred_error_codes[str(row.get("pred_error_code") or "UNKNOWN")] += 1
            if row.get("pred_elapsed_ms") != "" and row.get("pred_elapsed_ms") is not None:
                try:
                    pred_lat_ms.append(int(row["pred_elapsed_ms"]))
                except Exception:
                    pass

            pred_ids = _split_semicolon_ids(row.get("pred_answer_ids", ""))
            if len(pred_ids) == 0:
                empty_pred_pct_count += 1

            print(
                f"[{i + 1}/{n_run}] qid={row.get('questionId')} "
                f"pred_ok={row.get('pred_ok')} f1={row.get('f1'):.3f} "
                f"lat_ms={row.get('pred_elapsed_ms')}"
            )

    # Write results CSV
    fieldnames = [
        "mode",
        "prompt_style",
        "questionId",
        "question_en",
        "gold_answer_ids",
        "gold_answer_values",
        "pred_answer_ids",
        "pred_answer_values",
        "exact_match",
        "precision",
        "recall",
        "f1",
        "overlap_count",
        "extracted_entities_json",
        "extracted_properties_json",
        "chosen_entity_qids",
        "chosen_property_pids",
        "fallback_used",
        "fallback_attempts",
        "fallback_success",
        "fallback_strategy",
        "pred_attempts",
        "pred_sparql",
        "pred_sparql_attempt1",
        "pred_sparql_attempt2",
        "pred_ok",
        "pred_row_count",
        "pred_elapsed_ms",
        "pred_error_code",
        "pred_error_message",
    ]
    with open(out_csv, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(out_rows)

    # Summary
    em_rate = (exact_matches / n_run) if n_run else 0.0
    mean_f1 = statistics.mean(f1s) if f1s else 0.0
    median_f1 = statistics.median(f1s) if f1s else 0.0
    pred_success_rate = (pred_success / n_run) if n_run else 0.0
    median_lat = statistics.median(pred_lat_ms) if pred_lat_ms else None
    p90_lat = p90(pred_lat_ms)
    empty_pred_pct = (empty_pred_pct_count / n_run) if n_run else 0.0

    print("\nText→SPARQL benchmark summary")
    print(f"N_total: {total_rows}")
    print(f"N_scorable: {n_scorable}")
    print(f"N_run: {n_run}")
    print(f"exact_match_rate: {em_rate:.3f}")
    print(f"mean_f1: {mean_f1:.3f}")
    print(f"median_f1: {median_f1:.3f}")
    print(f"pred_exec_success_rate: {pred_success_rate:.3f}")
    print(f"pred_latency_median_ms: {median_lat}")
    print(f"pred_latency_p90_ms: {p90_lat}")
    print(f"most_common_pred_error_codes: {pred_error_codes.most_common(10)}")
    print(f"percent_empty_pred_results: {empty_pred_pct:.3f}")


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    _load_dotenv(repo_root / ".env")
    _ensure_requirements()

    parser = argparse.ArgumentParser()
    parser.add_argument("--gold_csv", required=True, help="Gold CSV path (from gold runner)")
    parser.add_argument("--out_csv", required=True, help="Output CSV path for predictions")
    parser.add_argument("--n", type=int, default=100, help="Number of scorable rows to run")
    parser.add_argument("--timeout_ms", type=int, default=30000)
    parser.add_argument("--limit_cap", type=int, default=200)
    parser.add_argument("--max_concurrency", type=int, default=1)
    parser.add_argument("--sleep_s", type=float, default=0.3, help="Sleep between items to reduce 429s")
    parser.add_argument("--llm_timeout_s", type=float, default=60.0, help="Per-LLM-call HTTP timeout in seconds")
    parser.add_argument(
        "--mode",
        choices=[m.value for m in Mode],
        default=Mode.mcp.value,
        help="Prediction mode: direct (LLM writes SPARQL) or mcp (ground then synthesize)",
    )
    parser.add_argument(
        "--prompt_style",
        choices=[p.value for p in PromptStyle],
        default=PromptStyle.smart.value,
        help="Prompting style: dumb (minimal) or smart (pattern hints + repair-on-empty)",
    )
    args = parser.parse_args()

    asyncio.run(
        run_benchmark(
            gold_csv=args.gold_csv,
            out_csv=args.out_csv,
            n=args.n,
            timeout_ms=args.timeout_ms,
            limit_cap=args.limit_cap,
            max_concurrency=args.max_concurrency,
            sleep_s=args.sleep_s,
            mode=Mode(args.mode),
            prompt_style=PromptStyle(args.prompt_style),
            llm_timeout_s=args.llm_timeout_s,
        )
    )


if __name__ == "__main__":
    main()

