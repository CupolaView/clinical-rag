import json

from eval.check_gold_sections import check, parse_gold_section


def test_parse_gold_section_normalises():
    assert parse_gold_section("Materials and Methods → Data Processing (Table 3)") == (
        ["materials and methods", "data processing"], "table 3")
    assert parse_gold_section("3. Results → 3.1. Auditory Cueing Devices (Table 2)") == (
        ["results", "auditory cueing devices"], "table 2")
    assert parse_gold_section("4. Discussion") == (["discussion"], "")
    assert parse_gold_section("Methods > Data acquisition") == (["methods", "data acquisition"], "")


def test_check_matches_subsections_and_labels_wherever_the_table_sits(tmp_path):
    chunks = [
        {"pmcid": "PMC1", "section_path": ["4. Discussion", "Limitations"], "section": "4. Discussion → Limitations", "label": ""},
        {"pmcid": "PMC1", "section_path": ["4. Discussion"], "section": "x", "label": "Table 3."},
    ]
    cp = tmp_path / "c.jsonl"
    cp.write_text("\n".join(json.dumps(c) for c in chunks) + "\n", encoding="utf-8")
    gp = tmp_path / "g.yaml"
    gp.write_text(
        "questions:\n"
        "  - id: a\n    sources:\n      - {pmcid: PMC1, section: 'Discussion'}\n"
        "      - {pmcid: PMC1, section: 'Materials and Methods → Data Processing (Table 3)'}\n"
        "  - id: b\n    sources:\n      - {pmcid: PMC1, section: 'Data Processing (Table 4)'}\n"
        "      - {pmcid: PMC2, section: 'Results'}\n"
        "  - id: c\n    sources: []\n",
        encoding="utf-8",
    )
    problems = check(gp, cp)
    assert len(problems) == 2
    assert "Table 4" in problems[0] and "PMC2" in problems[1]
