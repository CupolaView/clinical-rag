from pathlib import Path

from eval.validate_golden import validate

GOLDEN = Path(__file__).resolve().parents[1] / "eval" / "golden" / "golden_v0.yaml"


def test_golden_set_is_structurally_valid():
    """Runs in CI from day one. Coverage (--strict) is checked by hand until the set is complete."""
    assert validate(GOLDEN) == []


def test_manifest_check_reads_utf8_and_respects_status(tmp_path):
    golden = tmp_path / "g.yaml"
    golden.write_text(
        "questions:\n"
        "  - {id: a, question: q, type: factual, answerable: true, reference_answer: r,\n"
        "     sources: [{pmcid: PMC1, section: Methods}]}\n"
        "  - {id: b, question: q, type: factual, answerable: true, reference_answer: r,\n"
        "     sources: [{pmcid: PMC2, section: Methods}]}\n"
        "  - {id: c, question: q, type: factual, answerable: true, reference_answer: r,\n"
        "     sources: [{pmcid: PMC3, section: Methods}]}\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "m.csv"
    manifest.write_text(
        "pmcid,title,status\nPMC1,Parkinson\u2019s \u201cgait\u201d,ok\nPMC2,x,retracted\n",
        encoding="utf-8",
    )
    errs = validate(golden, manifest=manifest)
    assert len(errs) == 2
    assert "PMC2" in errs[0] and "retracted" in errs[0]
    assert "PMC3" in errs[1] and "not in" in errs[1]
