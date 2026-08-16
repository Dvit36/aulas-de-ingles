from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from english_leaderboard.image_processing import analyze_image_bytes
from english_leaderboard.ocr import OCRResult, create_ocr_engine, extract_text
from english_leaderboard.models import SubmissionStatus
from english_leaderboard.rules import (
    analyze_submission_rules,
    detect_completion,
    detect_platform,
)


def test_ocr_adapter_normalizes_legacy_result():
    engine = lambda _source: (
        [[None, "Atividade concluída", 0.98], [None, "Feedback", 0.88]],
        [0.01, 0.02, 0.03],
    )
    result = extract_text(b"fake image bytes", engine=engine)
    assert isinstance(result, OCRResult)
    assert "Atividade concluída" in result.text
    assert result.confidence == pytest.approx(0.93)


REAL_FIXTURES = [
    ("WhatsApp Image 2026-08-10 at 22.16.08.jpeg", "duolingo"),
    ("WhatsApp Image 2026-08-13 at 23.21.52.jpeg", "duolingo"),
    ("WhatsApp Image 2026-08-12 at 21.01.43 (1).jpeg", "beconfident"),
    ("WhatsApp Image 2026-08-13 at 22.50.47 (1).jpeg", "beconfident"),
]


@pytest.mark.ocr
@pytest.mark.skipif(os.getenv("RUN_OCR_TESTS") != "1", reason="ative com RUN_OCR_TESTS=1")
@pytest.mark.parametrize(("filename", "expected"), REAL_FIXTURES)
def test_real_screenshots_are_recognized(filename: str, expected: str):
    path = Path("inputs") / filename
    if not path.is_file():
        pytest.skip(f"fixture ausente: {filename}")
    analyzed = analyze_image_bytes(path.read_bytes())
    result = extract_text(analyzed.original_bytes, engine=create_ocr_engine())
    platform, _, _ = detect_platform(result.text, color_signals=analyzed.color_signals)
    completed, _, _ = detect_completion(result.text, platform)
    assert platform == expected, result.text
    assert completed, result.text
    activity = SimpleNamespace(
        code="duolingo_beconfident",
        requires_images=True,
        requires_title_or_url=False,
        requires_summary=False,
        content_review_required=False,
        auto_approvable=True,
    )
    decision = analyze_submission_rules(
        activity=activity,
        images=[analyzed],
        ocr_results=[result],
        exact_duplicate_flags=[False],
        similar_duplicate_flags=[False],
        auto_approve_confidence=0.85,
    )
    assert decision.status == SubmissionStatus.APPROVED_AUTO, decision.checks
    assert decision.recognized_units == 1
