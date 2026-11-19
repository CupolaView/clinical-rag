"""Check that every golden-set source (pmcid + section) maps to at least one chunk.

Run after ingest/parse.py. If a gold section has no chunk, retrieval can never "find"
it, so a low recall@k would be a parsing problem, not a retrieval problem. This catches
that before any retrieval code exists.

    python eval/check_gold_sections.py eval/golden/golden_v0.yaml data/processed/v1/chunks.jsonl

Matching ignores numbering ("4. Discussion" = "Discussion"), case, and arrow style
(→ or >). A gold section matches a chunk when its path appears, in order, inside the
chunk's section path; so "Discussion" also matches chunks in "Discussion → Limitations".
A gold source with a label such as "(Table 3)" matches the chunk with that label,
wherever the parser placed it (labels are unique within a paper).
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import yaml

_NUMBERING = re.compile(r"^\s*((\d+(\.\d+)*\.?)|([ivxlc]+\.)|([a-z]\.))\s+", re.IGNORECASE)
_LABEL = re.compile(r"\((table|figure|fig\.?)\s*([A-Za-z]?\d+[A-Za-z]?)\)\s*$", re.IGNORECASE)


def norm(part: str) -> str:
    s = part.replace("’", "'").replace("‘", "'").strip()
    s = _NUMBERING.sub("", s)
    return re.sub(r"\s+", " ", s).strip(" .:").lower()


def norm_label(label: str) -> str:
    m = re.match(r"\s*(table|figure|fig\.?)\s*([A-Za-z]?\d+[A-Za-z]?)", label or "", re.IGNORECASE)
    if not m:
        return ""
    kind = "table" if m.group(1).lower().startswith("t") else "figure"
    return f"{kind} {m.group(2).lower()}"


def parse_gold_section(section: str) -> tuple[list[str], str]:
    label = ""
    m = _LABEL.search(section)
    if m:
        label = norm_label(f"{m.group(1)} {m.group(2)}")
        section = section[: m.start()]
    parts = [norm(p) for p in re.split(r"\s*(?:→|->|>)\s*", section) if p.strip()]
    return parts, label


def contains_in_order(path: list[str], wanted: list[str]) -> bool:
    n = len(wanted)
    return any(path[i:i + n] == wanted for i in range(len(path) - n + 1)) if n else True


def matches(chunk: dict, wanted: list[str], label: str) -> bool:
    if label:
        # A table or figure is identified by its label, which is unique within a paper. Its
        # section is not reliable: PMC often stores floats at the end of the XML, and the
        # parser then borrows the section of the first paragraph that cites them.
        return norm_label(chunk.get("label", "")) == label
    path = [norm(p) for p in chunk["section_path"]]
    return contains_in_order(path, wanted)


def check(golden_path: Path, chunks_path: Path) -> list[str]:
    questions = yaml.safe_load(golden_path.read_text(encoding="utf-8"))["questions"]
    by_pmcid: dict[str, list[dict]] = defaultdict(list)
    with chunks_path.open(encoding="utf-8") as f:
        for line in f:
            c = json.loads(line)
            by_pmcid[c["pmcid"]].append(c)

    problems = []
    for q in questions:
        for s in q.get("sources") or []:
            pmcid, section = s["pmcid"], s["section"]
            chunks = by_pmcid.get(pmcid, [])
            if not chunks:
                problems.append(f"{q['id']}: no chunks at all for {pmcid} (not downloaded or not parsed?)")
                continue
            wanted, label = parse_gold_section(section)
            n = sum(matches(c, wanted, label) for c in chunks)
            if n == 0:
                have = sorted({c["section"] + (f" ({c['label']})" if c.get("label") else "") for c in chunks})
                problems.append(f"{q['id']}: {pmcid} '{section}' matches no chunk. Sections found: {have}")
            else:
                print(f"ok  {q['id']:26s} {pmcid:12s} {section}  [{n} chunk{'s' if n > 1 else ''}]")
    return problems


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    problems = check(Path(sys.argv[1]), Path(sys.argv[2]))
    if problems:
        print(f"\n{len(problems)} problem(s):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("\nAll gold sources map to at least one chunk.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
