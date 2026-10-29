from pathlib import Path

from eval.validate_golden import validate

GOLDEN = Path(__file__).resolve().parents[1] / "eval" / "golden" / "golden_v0.yaml"


def test_golden_set_is_structurally_valid():
    """Runs in CI from day one. Coverage (--strict) is checked by hand until the set is complete."""
    assert validate(GOLDEN) == []
