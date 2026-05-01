"""tests/unit/test_quality_gate.py — Quality gate and score parsing tests."""
import re

from nova.core.orchestrator import _parse_quality_score


def test_parse_score_colon():
    assert _parse_quality_score("SCORE: 85") == 85
    assert _parse_quality_score("score: 72") == 72
    assert _parse_quality_score("quality_score: 90") == 90
    assert _parse_quality_score("Quality Score: 60") == 60


def test_parse_score_out_of_100():
    assert _parse_quality_score("82/100") == 82
    assert _parse_quality_score("Score: 77 out of 100") == 77


def test_parse_score_bracket():
    assert _parse_quality_score("[SCORE=88]") == 88


def test_parse_score_none_when_missing():
    assert _parse_quality_score("This is a great article about climate change.") is None
    assert _parse_quality_score("") is None


def test_parse_score_out_of_range_ignored():
    # 101 is out of range [0, 100] — should return None
    assert _parse_quality_score("SCORE: 101") is None


def test_parse_score_in_context():
    output = """
    ## Review

    The article covers the topic well with clear structure.

    SCORE: 78

    Improvements: add more citations.
    """
    assert _parse_quality_score(output) == 78


def test_parse_score_zero():
    assert _parse_quality_score("quality_score: 0") == 0


def test_parse_score_100():
    assert _parse_quality_score("SCORE: 100") == 100
