"""Validate the golden question set.

Default mode checks structure only, so CI stays green while the set is being written.
--strict also enforces the Week 0 coverage targets.
--manifest checks that every cited PMCID is in the frozen corpus manifest (use from Nov on).

    python eval/validate_golden.py eval/golden/golden_v0.yaml --strict
    python eval/validate_golden.py eval/golden/golden_v0.yaml --manifest ingest/manifest_v1.csv
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter
from pathlib import Path

import yaml

TYPES = {"factual", "comparative", "numeric_table", "multi_paper"}
UNANSWERABLE_REASONS = {"out_of_corpus", "patient_specific"}
DIFFICULTIES = {"easy", "medium", "hard"}
PMCID_RE = re.compile(r"^PMC\d+$")

# Week 0 coverage targets (--strict)
MIN_QUESTIONS = 20
MIN_PER_ANSWERABLE_TYPE = 3
MIN_UNANSWERABLE = 3
REQUIRED_REASONS = UNANSWERABLE_REASONS  # at least one of each


def load(path: Path) -> list[dict]:
    data = yaml.safe_load(path.read_text()) or {}
    questions = data.get("questions") or []
    if not isinstance(questions, list):
        raise SystemExit(f"{path}: 'questions' must be a list")
    return questions


def check_question(q: dict) -> list[str]:
    qid = q.get("id", "<missing id>")
    errs: list[str] = []

    def err(msg: str) -> None:
        errs.append(f"{qid}: {msg}")

    for field in ("id", "question", "type", "answerable", "reference_answer", "sources"):
        if field not in q:
            err(f"missing field '{field}'")
    if errs:
        return errs

    if not str(q["question"]).strip():
        err("empty question")
    if not str(q["reference_answer"]).strip():
        err("empty reference_answer")
    if q["type"] not in TYPES:
        err(f"type '{q['type']}' not in {sorted(TYPES)}")
    if "difficulty" in q and q["difficulty"] not in DIFFICULTIES:
        err(f"difficulty '{q['difficulty']}' not in {sorted(DIFFICULTIES)}")
    if not isinstance(q["answerable"], bool):
        err("answerable must be true or false")
        return errs

    sources = q["sources"] or []
    if q["answerable"]:
        if not sources:
            err("answerable question needs at least one source")
        for s in sources:
            pmcid = str((s or {}).get("pmcid", ""))
            if not PMCID_RE.match(pmcid):
                err(f"bad pmcid '{pmcid}' (expected PMC followed by digits)")
            if not str((s or {}).get("section", "")).strip():
                err(f"source {pmcid} missing section")
        if q["type"] == "multi_paper" and len({s.get("pmcid") for s in sources}) < 2:
            err("multi_paper needs sources from at least 2 distinct PMCIDs")
        if "unanswerable_reason" in q:
            err("unanswerable_reason set on an answerable question")
    else:
        if sources:
            err("unanswerable question should have sources: []")
        if q.get("unanswerable_reason") not in UNANSWERABLE_REASONS:
            err(f"unanswerable_reason must be one of {sorted(UNANSWERABLE_REASONS)}")
    return errs


def coverage_errors(questions: list[dict]) -> list[str]:
    errs: list[str] = []
    if any("PLACEHOLDER" in str(q.get("question", "")) for q in questions):
        errs.append("placeholder example entries are still present; delete them")
    if len(questions) < MIN_QUESTIONS:
        errs.append(f"only {len(questions)} questions; need at least {MIN_QUESTIONS}")
    answerable_types = Counter(q["type"] for q in questions if q.get("answerable"))
    for t in sorted(TYPES):
        if answerable_types[t] < MIN_PER_ANSWERABLE_TYPE:
            errs.append(f"type '{t}': {answerable_types[t]} answerable; need {MIN_PER_ANSWERABLE_TYPE}")
    unanswerable = [q for q in questions if q.get("answerable") is False]
    if len(unanswerable) < MIN_UNANSWERABLE:
        errs.append(f"only {len(unanswerable)} unanswerable; need {MIN_UNANSWERABLE}")
    reasons = {q.get("unanswerable_reason") for q in unanswerable}
    for r in sorted(REQUIRED_REASONS - reasons):
        errs.append(f"no unanswerable question with reason '{r}'")
    return errs


def manifest_errors(questions: list[dict], manifest: Path) -> list[str]:
    with manifest.open(newline="") as f:
        corpus = {row["pmcid"] for row in csv.DictReader(f)}
    errs = []
    for q in questions:
        for s in q.get("sources") or []:
            if s.get("pmcid") not in corpus:
                errs.append(f"{q['id']}: {s.get('pmcid')} is not in {manifest.name}")
    return errs


def summarize(questions: list[dict]) -> str:
    ans = [q for q in questions if q.get("answerable")]
    unans = [q for q in questions if q.get("answerable") is False]
    by_type = Counter(q.get("type") for q in ans)
    by_reason = Counter(q.get("unanswerable_reason") for q in unans)
    pmcids = {s.get("pmcid") for q in ans for s in (q.get("sources") or [])}
    lines = [
        f"{len(questions)} questions: {len(ans)} answerable, {len(unans)} unanswerable",
        "  answerable by type: " + ", ".join(f"{t}={by_type[t]}" for t in sorted(TYPES)),
        "  unanswerable by reason: " + ", ".join(f"{r}={by_reason[r]}" for r in sorted(UNANSWERABLE_REASONS)),
        f"  distinct gold PMCIDs: {len(pmcids)}",
    ]
    return "\n".join(lines)


def validate(path: Path, strict: bool = False, manifest: Path | None = None) -> list[str]:
    questions = load(path)
    errs: list[str] = []
    ids = Counter(q.get("id") for q in questions)
    errs += [f"duplicate id '{i}'" for i, n in ids.items() if n > 1]
    for q in questions:
        errs += check_question(q)
    if strict:
        errs += coverage_errors(questions)
    if manifest:
        errs += manifest_errors(questions, manifest)
    return errs


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("path", type=Path)
    p.add_argument("--strict", action="store_true", help="enforce Week 0 coverage targets")
    p.add_argument("--manifest", type=Path, help="corpus manifest CSV with a 'pmcid' column")
    args = p.parse_args()

    errs = validate(args.path, args.strict, args.manifest)
    print(summarize(load(args.path)))
    if errs:
        print(f"\n{len(errs)} problem(s):")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
